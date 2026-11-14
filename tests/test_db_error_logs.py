"""A database error is logged as its first line and its stack frames, never its message body.

Postgres puts the values of the row under the first line of an error (`DETAIL:  Failing row contains
(...)`, `Key (col)=(value) ...`), and SQLAlchemy appends the statement. Those are prompts, file_url,
metadata, usernames, password hashes: no secret may reach a log (or the /healthz migration status).
The unhandled-error handler, the startup migration failure, the orphaned-preview delete and the
stale-hold reaper are every place the store logs the exception of a database call. uvicorn logs each
unhandled error again on its own (uvicorn.error, with exc_info), so the JSON formatter applies the
same rule to every record, whoever logged it.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import uvicorn
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from app import main as app_main
from app.logging_config import init_logging, log_exception
from app.migration_state import get_migration_status
from tests.conftest import USER_A

SECRET_ROW = "SECRET-ROW-456"
FIRST_LINE = 'null value in column "prompt" of relation "media_items" violates not-null constraint'


class NotNullViolationError(Exception):
    """Stands in for the driver's error (asyncpg's): a first line, then Postgres' DETAIL line."""


def _db_error() -> IntegrityError:
    orig = NotNullViolationError(f"{FIRST_LINE}\nDETAIL:  Failing row contains ({SECRET_ROW}, a prompt, ...).")
    return IntegrityError("INSERT INTO media_items (id, prompt) VALUES (%s, %s)", (SECRET_ROW, "a prompt"), orig)


def _refuse():
    raise _db_error()


def _json_lines(stdout: str) -> list[dict]:
    return [json.loads(line) for line in stdout.splitlines() if line.startswith("{")]


def _assert_only_the_first_line(out: str, line: dict) -> None:
    """The one line `line` and the whole of stdout carry the error class and the first line, the frames
    when there are any, and none of the body."""
    assert SECRET_ROW not in out
    assert "DETAIL" not in out and "[SQL" not in out and "[parameters" not in out
    assert "NotNullViolationError" in line["exc"] and FIRST_LINE in line["msg"]


def _break_the_store(env, monkeypatch, raising) -> TestClient:
    from app.stores import media as media_store

    async def get_media_that_fails(session, media_id, user_id=None):
        raising()

    monkeypatch.setattr(media_store, "get_media", get_media_that_fails)
    return TestClient(env.client.app, raise_server_exceptions=False)


def _chained():
    try:
        _refuse()
    except IntegrityError as exc:
        raise RuntimeError("could not save") from exc


def _get_media(client, env):
    return client.get(f"/v1/media/{uuid.uuid4()}", headers=env.headers(USER_A))


def test_an_unhandled_database_error_logs_its_class_first_line_and_frames_only(
    env, monkeypatch, capsys, restore_logging
):
    client = _break_the_store(env, monkeypatch, _refuse)
    init_logging("store")

    resp = _get_media(client, env)

    assert (resp.status_code, resp.json()) == (500, {"error": "internal_server_error"})
    out = capsys.readouterr().out
    [line] = [x for x in _json_lines(out) if x["msg"].startswith("unhandled error")]
    assert (line["level"], line["logger"]) == ("error", "app.fastapi_app")
    assert line["msg"] == f"unhandled error: NotNullViolationError: {FIRST_LINE}"
    assert line["exc"].startswith("Traceback (most recent call last):")
    assert "in _refuse" in line["exc"]  # the frames are there
    _assert_only_the_first_line(out, line)


def test_a_database_error_behind_another_exception_is_logged_the_same_way(
    env, monkeypatch, capsys, restore_logging
):
    client = _break_the_store(env, monkeypatch, _chained)
    init_logging("store")

    assert _get_media(client, env).status_code == 500

    out = capsys.readouterr().out
    [line] = [x for x in _json_lines(out) if x["msg"].startswith("unhandled error")]
    assert "in _chained" in line["exc"] and "in _refuse" in line["exc"]
    _assert_only_the_first_line(out, line)


def test_uvicorns_own_line_for_an_unhandled_error_is_cut_down_the_same_way(capsys, restore_logging):
    """What uvicorn does for every exception that escapes the app (the store's handler re-raises it)."""
    init_logging("store")
    try:
        _refuse()
    except IntegrityError as exc:
        logging.getLogger("uvicorn.error").error("Exception in ASGI application\n", exc_info=exc)

    out = capsys.readouterr().out
    [line] = _json_lines(out)
    assert line["logger"] == "uvicorn.error" and line["msg"].startswith("Exception in ASGI application")
    assert "in _refuse" in line["exc"] and "NotNullViolationError" in line["exc"]
    assert SECRET_ROW not in out and "DETAIL" not in out and "[SQL" not in out


def test_a_real_uvicorn_server_logs_the_database_error_twice_and_leaks_neither(
    env, monkeypatch, capsys, restore_logging
):
    """The store's own line and uvicorn's, from a request over a socket (app.main's settings)."""
    _break_the_store(env, monkeypatch, _refuse)
    init_logging("store")
    server = uvicorn.Server(
        uvicorn.Config(env.client.app, host="127.0.0.1", port=0, log_config=None, access_log=False, lifespan="off")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        assert server.started
        port = server.servers[0].sockets[0].getsockname()[1]
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/media/{uuid.uuid4()}", headers=env.headers(USER_A)
        )
        try:
            urllib.request.urlopen(request, timeout=10)
        except urllib.error.HTTPError as refused:  # the 500 is the point
            status, body = refused.code, json.loads(refused.read())
    finally:
        server.should_exit = True
        thread.join(timeout=10)

    assert (status, body) == (500, {"error": "internal_server_error"})
    out = capsys.readouterr().out
    loggers = {x["logger"] for x in _json_lines(out) if x["level"] == "error"}
    assert loggers == {"app.fastapi_app", "uvicorn.error"}
    assert SECRET_ROW not in out and "DETAIL" not in out and "[SQL" not in out


def test_other_exceptions_keep_their_traceback_through_log_exception(capsys, restore_logging):
    init_logging("store")
    log = logging.getLogger("app.stores.credits_reaper")
    try:
        raise ValueError("plain failure")
    except ValueError as exc:
        log_exception(log, logging.WARNING, "could not do %s", "x", exc=exc)

    [line] = _json_lines(capsys.readouterr().out)
    assert line["msg"] == "could not do x"
    assert "Traceback" in line["exc"] and "ValueError: plain failure" in line["exc"]


def test_a_failed_migration_logs_and_reports_the_first_line_only(
    monkeypatch, capsys, restore_logging, restore_migration_status
):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://x:y@nowhere/db")
    init_logging("store")
    with patch.object(app_main, "_database_state", return_value="stamped"), \
         patch.object(app_main, "_stamped_version", return_value="0014"), \
         patch("alembic.command.upgrade", side_effect=_db_error()):
        app_main.run_migrations()  # must not raise

    out = capsys.readouterr().out
    [line] = [x for x in _json_lines(out) if x["msg"].startswith("alembic: migration failed")]
    assert line["level"] == "critical"
    _assert_only_the_first_line(out, line)
    status = get_migration_status()  # what /healthz tells any caller
    assert status == f"failed: IntegrityError: NotNullViolationError: {FIRST_LINE}"
    assert SECRET_ROW not in status


def test_a_failed_orphan_preview_delete_logs_the_first_line_only(monkeypatch, capsys, restore_logging):
    import asyncio

    from app.stores import clipstyle_packages as cp

    class Nested:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    async def delete_that_fails(session, user_id, ids):
        _refuse()

    session = MagicMock()
    session.begin_nested = Nested
    monkeypatch.setattr(cp.media_store, "delete_clipstyle_preview_media", delete_that_fails)
    monkeypatch.setattr(cp, "_bridge_media_ids", lambda report: ["media-1"] if report else [])
    init_logging("store")

    kept = asyncio.run(cp.drop_stale_preview_media(session, "tpl-1", "user-1", {"old": True}, None))

    assert kept == frozenset({"media-1"})
    out = capsys.readouterr().out
    [line] = _json_lines(out)
    assert line["msg"].startswith("clipstyle package tpl-1: could not delete 1 orphaned preview media for user user-1")
    assert "in _refuse" in line["exc"]
    _assert_only_the_first_line(out, line)


def test_a_failed_stale_hold_release_logs_the_first_line_only(monkeypatch, capsys, restore_logging):
    import asyncio

    from app.stores import credits, credits_reaper

    async def release_that_fails(session, **kwargs):
        _refuse()

    stale = MagicMock()
    stale.all.return_value = [(USER_A, "run-1", "operator_chat", 1, 5)]
    session = MagicMock()
    session.execute = AsyncMock(return_value=stale)
    session.rollback = AsyncMock()
    monkeypatch.setattr(credits, "release", release_that_fails)
    rules = [{"checkpoint": "operator_chat", "match": "exact", "older_than_s": 3600}]
    init_logging("store")

    reaped = asyncio.run(
        credits_reaper.reap_stale_holds(session, rules, now=datetime(2026, 11, 14, tzinfo=timezone.utc))
    )

    assert reaped == []
    session.rollback.assert_awaited_once()
    out = capsys.readouterr().out
    [line] = _json_lines(out)
    assert line["msg"].startswith("stale-hold reaper: could not release hold run=run-1 checkpoint=operator_chat")
    assert "in _refuse" in line["exc"]
    _assert_only_the_first_line(out, line)
