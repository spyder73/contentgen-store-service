"""Verifies the X-Internal-Secret gate — every /v1/* route must reject
requests that lack the header, and must let through requests that carry it."""
from __future__ import annotations

import os
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

INTERNAL_SECRET = "test-secret-xyz"
os.environ["INTERNAL_API_SECRET"] = INTERNAL_SECRET

from app.db import get_session
from app.fastapi_app import create_fastapi_app


async def _mock_session():
    yield MagicMock()


@pytest.fixture()
def client():
    app = create_fastapi_app()
    app.dependency_overrides[get_session] = _mock_session
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


@pytest.fixture(autouse=True)
def _keep_secret(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_SECRET", INTERNAL_SECRET)


# Representative sample covering every resource family + verb. If the gate
# regresses, these tests light up before any business-logic test does.
SAMPLE_ROUTES = [
    ("get", "/v1/pipelines"),
    ("get", "/v1/prompts"),
    ("get", "/v1/render-templates"),
    ("get", "/v1/render-proposals"),
    ("get", "/v1/clips"),
    ("get", "/v1/media"),
    ("get", "/v1/media/stats"),
    ("get", "/v1/series"),
    ("get", "/v1/characters"),
    ("get", "/v1/episodes"),
    ("get", "/v1/voice-snippets"),
]


class TestInternalSecretGate:
    @pytest.mark.parametrize("method,path", SAMPLE_ROUTES)
    def test_missing_secret_returns_401(self, client, method, path):
        resp = getattr(client, method)(path, headers={"X-User-ID": str(uuid.uuid4())})
        assert resp.status_code == 401, f"{method.upper()} {path}"

    @pytest.mark.parametrize("method,path", SAMPLE_ROUTES)
    def test_wrong_secret_returns_401(self, client, method, path):
        resp = getattr(client, method)(
            path,
            headers={"X-User-ID": str(uuid.uuid4()), "X-Internal-Secret": "wrong"},
        )
        assert resp.status_code == 401, f"{method.upper()} {path}"

    def test_credits_reserve_missing_secret_401(self, client):
        uid = str(uuid.uuid4())
        resp = client.post(
            f"/v1/users/{uid}/credits/reserve",
            headers={"X-User-ID": uid},
            json={
                "amount_credits": 10,
                "pipeline_run_id": str(uuid.uuid4()),
                "checkpoint_id": "cp",
                "attempt": 1,
                "idempotency_key": "k",
            },
        )
        assert resp.status_code == 401

    def test_admin_grant_missing_secret_401(self, client):
        resp = client.post(
            "/v1/internal/admin/credits/grant",
            headers={"X-User-ID": str(uuid.uuid4())},
            json={"user_id": str(uuid.uuid4()), "amount_credits": 1000, "note": "topup"},
        )
        assert resp.status_code == 401

    def test_healthz_bypasses_gate(self, client):
        # /healthz is outside /v1/* and must remain reachable for probes.
        resp = client.get("/healthz")
        assert resp.status_code == 200

    def test_secret_accepted(self, client):
        from app.schemas import PagedResponse

        paged = PagedResponse(items=[], total=0, page=1, limit=50)
        with patch("app.stores.media.list_media", new=AsyncMock(return_value=paged)):
            resp = client.get(
                "/v1/media",
                headers={
                    "X-User-ID": str(uuid.uuid4()),
                    "X-Internal-Secret": INTERNAL_SECRET,
                },
            )
        assert resp.status_code == 200
