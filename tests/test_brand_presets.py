from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

INTERNAL_SECRET = "test-secret-xyz"
os.environ["INTERNAL_API_SECRET"] = INTERNAL_SECRET

from app.db import get_session
from app.fastapi_app import create_fastapi_app
from app.schemas import BrandPresetOut


async def _mock_session():
    yield MagicMock()


@pytest.fixture(autouse=True)
def _keep_secret(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_SECRET", INTERNAL_SECRET)


@pytest.fixture()
def client():
    app = create_fastapi_app()
    app.dependency_overrides[get_session] = _mock_session
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


def _headers(user_id: str) -> dict[str, str]:
    return {"X-User-ID": user_id, "X-Internal-Secret": INTERNAL_SECRET}


def _preset_out(id_: str, user_id: str) -> BrandPresetOut:
    now = datetime.now(timezone.utc)
    return BrandPresetOut(
        id=id_,
        clip_style="genericCarousel",
        name="Client Physics",
        brand_tag="clientphysics",
        preset_json={"highlightStart": "#123456"},
        user_id=user_id,
        created_at=now,
        updated_at=now,
    )


def test_list_brand_presets_forwards_user_id_and_clip_style(client):
    uid = str(uuid.uuid4())
    with patch("app.stores.brand_presets.list_brand_presets", new=AsyncMock(return_value=[])) as m:
        resp = client.get("/v1/brand-presets?clip_style=genericCarousel", headers=_headers(uid))

    assert resp.status_code == 200
    assert m.call_args.kwargs["user_id"] == uid
    assert m.call_args.kwargs["clip_style"] == "genericCarousel"


def test_create_brand_preset_is_user_scoped(client):
    uid = str(uuid.uuid4())
    preset_id = str(uuid.uuid4())
    out = _preset_out(preset_id, uid)
    with patch("app.stores.brand_presets.create_brand_preset", new=AsyncMock(return_value=out)) as m:
        resp = client.post(
            "/v1/brand-presets",
            headers=_headers(uid),
            json={
                "clip_style": "genericCarousel",
                "name": "Client Physics",
                "brand_tag": "clientphysics",
                "preset_json": {"highlightStart": "#123456"},
            },
        )

    assert resp.status_code == 200
    assert resp.json()["id"] == preset_id
    assert m.call_args.kwargs["user_id"] == uid


def test_update_brand_preset_returns_404_for_other_user(client):
    uid = str(uuid.uuid4())
    preset_id = str(uuid.uuid4())
    with patch("app.stores.brand_presets.update_brand_preset", new=AsyncMock(return_value=None)):
        resp = client.patch(
            f"/v1/brand-presets/{preset_id}",
            headers=_headers(uid),
            json={"name": "Updated"},
        )

    assert resp.status_code == 404


def test_delete_brand_preset_returns_404_for_other_user(client):
    uid = str(uuid.uuid4())
    preset_id = str(uuid.uuid4())
    with patch("app.stores.brand_presets.delete_brand_preset", new=AsyncMock(return_value=False)):
        resp = client.delete(f"/v1/brand-presets/{preset_id}", headers=_headers(uid))

    assert resp.status_code == 404


def test_brand_presets_require_user_id(client):
    resp = client.get("/v1/brand-presets", headers={"X-Internal-Secret": INTERNAL_SECRET})

    assert resp.status_code == 401
