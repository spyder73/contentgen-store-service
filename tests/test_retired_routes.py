"""The pipeline template, prompt-template and run-snapshot routes are retired (roadmap 6)."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app.db import get_session
from app.fastapi_app import create_fastapi_app

INTERNAL_SECRET = "test-secret-xyz"
HEADERS = {"X-User-ID": "00000000-0000-0000-0000-0000000000a1", "X-Internal-Secret": INTERNAL_SECRET}

RETIRED = [
    ("get", "/v1/pipelines"),
    ("get", "/v1/pipelines/p1"),
    ("put", "/v1/pipelines/p1"),
    ("delete", "/v1/pipelines/p1"),
    ("get", "/v1/run-snapshots"),
    ("get", "/v1/run-snapshots/s1"),
    ("put", "/v1/run-snapshots/s1"),
    ("delete", "/v1/run-snapshots/s1"),
    ("delete", "/v1/run-snapshots"),
    ("get", "/v1/prompts"),
    ("get", "/v1/prompts/x"),
    ("put", "/v1/prompts/x"),
    ("delete", "/v1/prompts/x"),
    ("get", "/v1/internal/admin/pipelines"),
    ("put", "/v1/internal/admin/pipelines/p1"),
    ("patch", "/v1/internal/admin/pipelines/p1/visibility"),
    ("put", "/v1/internal/admin/pipelines/p1/assignments"),
    ("delete", "/v1/internal/admin/pipelines/p1"),
    ("get", "/v1/internal/admin/prompts"),
    ("put", "/v1/internal/admin/prompts/x"),
    ("put", "/v1/admin/prompt-templates/x"),
]


async def _mock_session():
    yield MagicMock()


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_SECRET", INTERNAL_SECRET)
    app = create_fastapi_app()
    app.dependency_overrides[get_session] = _mock_session
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def test_pipeline_template_prompt_and_snapshot_routes_are_gone(client):
    for method, path in RETIRED:
        assert getattr(client, method)(path, headers=HEADERS).status_code in (404, 405), (method, path)
    # The live system-prompt routes are still registered.
    paths = {r.path for r in client.app.routes}
    assert "/v1/system-prompts/{id}" in paths
    assert not any(p.startswith(("/v1/pipelines", "/v1/run-snapshots", "/v1/prompts")) for p in paths)
    methods = {m for r in client.app.routes if r.path == "/v1/system-prompts/{id}" for m in r.methods}
    assert {"GET", "PUT"} <= methods


def test_alembic_still_has_one_head_0056():
    root = Path(__file__).resolve().parents[1]
    out = subprocess.run([sys.executable, "-m", "alembic", "heads"], cwd=root, capture_output=True, text=True, check=True)
    heads = [l for l in out.stdout.splitlines() if l.strip()]
    assert len(heads) == 1 and heads[0].startswith("0056"), out.stdout
