from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.db import get_session
from app.fastapi_app import create_fastapi_app
from app.schemas import PuppetPosePresetOut

SECRET = "pose-test-secret"
USER_ID = "00000000-0000-0000-0000-000000000123"
POSE_ID = "00000000-0000-0000-0000-000000000456"


async def _session():
    yield MagicMock()


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_SECRET", SECRET)
    app = create_fastapi_app()
    app.dependency_overrides[get_session] = _session
    with TestClient(app) as value:
        yield value


def _headers(user_id: str = USER_ID):
    return {"X-Internal-Secret": SECRET, "X-User-ID": user_id}


def _pose() -> PuppetPosePresetOut:
    now = datetime.now(timezone.utc)
    return PuppetPosePresetOut(
        id=POSE_ID,
        user_id=USER_ID,
        name="Hero pose",
        prompt_hint="balancing on one leg",
        config={"source": "puppet", "renderer_version": 3, "pose_preset": "custom"},
        created_at=now,
        updated_at=now,
    )


def test_pose_library_requires_user_header(client):
    response = client.get("/v1/puppet-poses", headers={"X-Internal-Secret": SECRET})
    assert response.status_code == 401


def test_list_is_scoped_to_header_user(client):
    with patch("app.stores.puppet_pose_presets.list_presets", new=AsyncMock(return_value=[_pose()])) as call:
        response = client.get("/v1/puppet-poses", headers=_headers())
    assert response.status_code == 200
    assert response.json()[0]["prompt_hint"] == "balancing on one leg"
    assert call.call_args.args[1] == USER_ID


def test_create_normalizes_text_and_stamps_header_user(client):
    with patch("app.stores.puppet_pose_presets.create_preset", new=AsyncMock(return_value=_pose())) as call:
        response = client.post(
            "/v1/puppet-poses",
            headers=_headers(),
            json={
                "name": "  Hero   pose ",
                "prompt_hint": " balancing   on one leg ",
                "config": {"source": "puppet", "pose_preset": "custom"},
            },
        )
    assert response.status_code == 200
    assert call.call_args.args[1] == USER_ID
    body = call.call_args.args[2]
    assert body.name == "Hero pose"
    assert body.prompt_hint == "balancing on one leg"


def test_delete_cannot_cross_user_boundary(client):
    with patch("app.stores.puppet_pose_presets.delete_preset", new=AsyncMock(return_value=False)) as call:
        response = client.delete(f"/v1/puppet-poses/{POSE_ID}", headers=_headers())
    assert response.status_code == 404
    assert call.call_args.args[2] == USER_ID
