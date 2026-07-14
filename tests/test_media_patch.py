"""Tests for the PATCH /v1/media/{id} partial-update endpoint.

The endpoint backs the go-backend's targeted file_url write (bytes goroutine)
and the settle goroutine's cost_credits metadata merge — two disjoint concerns
that must be applyable independently without a full-row upsert.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

INTERNAL_SECRET = "test-secret-xyz"


@pytest.fixture(autouse=True)
def _internal_secret_env(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_SECRET", INTERNAL_SECRET)


from app.fastapi_app import create_fastapi_app
from app.schemas import MediaItemOut
from app.db import get_session

AUTH_HEADERS = {
    "X-Internal-Secret": INTERNAL_SECRET,
    "X-User-ID": "user-1",
}


def _make_media_out(id: str, metadata: dict | None = None, file_url: str = "u") -> MediaItemOut:
    now = datetime.now(timezone.utc)
    return MediaItemOut(
        id=id,
        clip_id=None,
        type="image",
        prompt="p",
        file_url=file_url,
        metadata=metadata or {},
        output_spec=None,
        is_favourite=False,
        created_at=now,
        updated_at=now,
    )


async def _mock_session():
    yield MagicMock()


@pytest.fixture()
def client():
    app = create_fastapi_app()
    app.dependency_overrides[get_session] = _mock_session
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


class TestPatchMediaEndpoint:
    def test_patch_metadata_merge_forwarded(self, client):
        media_id = str(uuid.uuid4())
        expected = _make_media_out(media_id, metadata={"cost_credits": 34})

        with patch("app.stores.media.patch_media", new=AsyncMock(return_value=expected)) as mock_patch:
            resp = client.patch(
                f"/v1/media/{media_id}",
                json={"metadata_merge": {"cost_credits": 34}},
                headers=AUTH_HEADERS,
            )

        assert resp.status_code == 200
        assert resp.json()["metadata"]["cost_credits"] == 34
        # file_url omitted → None; metadata_merge forwarded verbatim.
        _, kwargs = mock_patch.call_args
        assert kwargs["file_url"] is None
        assert kwargs["metadata_merge"] == {"cost_credits": 34}
        assert kwargs["user_id"] == "user-1"

    def test_patch_file_url_forwarded(self, client):
        media_id = str(uuid.uuid4())
        expected = _make_media_out(media_id, file_url="/managed/x.png")

        with patch("app.stores.media.patch_media", new=AsyncMock(return_value=expected)) as mock_patch:
            resp = client.patch(
                f"/v1/media/{media_id}",
                json={"file_url": "/managed/x.png"},
                headers=AUTH_HEADERS,
            )

        assert resp.status_code == 200
        _, kwargs = mock_patch.call_args
        assert kwargs["file_url"] == "/managed/x.png"
        assert kwargs["metadata_merge"] == {}

    def test_patch_not_found(self, client):
        media_id = str(uuid.uuid4())
        with patch("app.stores.media.patch_media", new=AsyncMock(return_value=None)):
            resp = client.patch(
                f"/v1/media/{media_id}",
                json={"metadata_merge": {"cost_credits": 1}},
                headers=AUTH_HEADERS,
            )
        assert resp.status_code == 404
