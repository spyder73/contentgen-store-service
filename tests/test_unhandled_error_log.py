"""An unhandled error in the store reaches stdout as one JSON log line.

On 2026-11-14 a media PUT answered 500 and `docker compose logs store | grep -i error` showed
nothing. Cause: startup runs `alembic upgrade head` after init_logging, and alembic/env.py called
`fileConfig(alembic.ini)`, which replaced the root handler with a plain stderr one (level WARN) and
disabled every logger that already existed (app.fastapi_app, uvicorn.error, uvicorn.access, ...).
app.main now tells env.py not to (`configure_logger=False`).
"""
from __future__ import annotations

import json
import logging

import pytest
from fastapi.testclient import TestClient

from app import main as app_main
from app.logging_config import JSONFormatter, init_logging
from tests.conftest import USER_A


@pytest.fixture()
def restore_logging():
    """init_logging and alembic's fileConfig rewrite process-wide logging: put it back afterwards."""
    root = logging.getLogger()
    saved_root = (list(root.handlers), root.level)
    saved = {
        name: (list(lg.handlers), lg.level, lg.propagate, lg.disabled)
        for name, lg in logging.root.manager.loggerDict.items()
        if isinstance(lg, logging.Logger)
    }
    yield
    root.handlers[:], root.level = saved_root[0], saved_root[1]
    for name, (handlers, level, propagate, disabled) in saved.items():
        lg = logging.getLogger(name)
        lg.handlers[:], lg.level, lg.propagate, lg.disabled = handlers, level, propagate, disabled


def _break_the_store(env, monkeypatch) -> TestClient:
    """A client whose GET /v1/media/{id} hits an exception nothing handles (a store call that raises)."""
    from app.stores import media as media_store

    async def boom(session, media_id, user_id=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(media_store, "get_media", boom)
    return TestClient(env.client.app, raise_server_exceptions=False)


def _unhandled_lines(stdout: str) -> list[dict]:
    lines = [json.loads(line) for line in stdout.splitlines() if line.startswith("{")]
    return [line for line in lines if line["msg"].startswith("unhandled error")]


def _get_media(client: TestClient, env):
    return client.get("/v1/media/11111111-1111-1111-1111-111111111111", headers=env.headers(USER_A))


def test_an_unhandled_exception_is_one_json_line_on_stdout(env, monkeypatch, capsys, restore_logging):
    client = _break_the_store(env, monkeypatch)
    init_logging("store")  # after capsys: the handler binds the captured stdout

    resp = _get_media(client, env)

    assert (resp.status_code, resp.json()) == (500, {"error": "internal_server_error"})
    [line] = _unhandled_lines(capsys.readouterr().out)
    assert line["level"] == "error" and line["logger"] == "app.fastapi_app"
    assert line["msg"] == "unhandled error: boom"
    assert "RuntimeError: boom" in line["exc"]


def test_startup_migrations_leave_the_logging_that_init_logging_set_up(
    env, monkeypatch, tmp_path, capsys, restore_logging
):
    """The order app.main uses: init_logging, then run_migrations (which runs alembic/env.py for real;
    against sqlite the first migration fails, which run_migrations survives)."""
    client = _break_the_store(env, monkeypatch)
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'startup.db'}")
    init_logging("store")
    app_main.run_migrations()

    for name in ("app.fastapi_app", "uvicorn.error", "uvicorn.access"):
        assert not logging.getLogger(name).disabled, name
    [handler] = logging.getLogger().handlers
    assert isinstance(handler.formatter, JSONFormatter)

    resp = _get_media(client, env)

    assert resp.status_code == 500
    [line] = _unhandled_lines(capsys.readouterr().out)
    assert line["msg"] == "unhandled error: boom" and "exc" in line
