"""Log volume: /healthz (polled every few seconds by the container healthcheck) writes no request
line, a normal request writes one, and uvicorn's access line, which repeats the request middleware's,
is off."""
from __future__ import annotations

import json
import uuid

from app import main as app_main
from app.logging_config import init_logging
from tests.conftest import USER_A


def _lines(stdout: str, msg_prefix: str) -> list[dict]:
    lines = [json.loads(line) for line in stdout.splitlines() if line.startswith("{")]
    return [line for line in lines if line["msg"].startswith(msg_prefix)]


def test_healthz_writes_no_request_line_and_a_normal_request_writes_one(env, capsys, restore_logging):
    init_logging("store")

    assert env.client.get("/healthz").status_code == 200
    assert _lines(capsys.readouterr().out, "request") == []

    resp = env.client.get(f"/v1/media/{uuid.uuid4()}", headers=env.headers(USER_A))

    assert resp.status_code == 404
    [line] = _lines(capsys.readouterr().out, "request")
    assert (line["method"], line["path"].startswith("/v1/media/")) == ("GET", True)
    assert "latency_ms" in line


def test_uvicorn_runs_without_its_access_log(monkeypatch):
    """The request middleware already logs method, path and latency once per request."""
    seen: dict = {}
    monkeypatch.setattr(app_main, "init_logging", lambda service: None)
    monkeypatch.setattr(app_main, "run_migrations", lambda: None)
    monkeypatch.setattr(app_main.uvicorn, "run", lambda app, **kwargs: seen.update(kwargs))

    app_main.main()

    assert seen["access_log"] is False
    assert seen["log_config"] is None
