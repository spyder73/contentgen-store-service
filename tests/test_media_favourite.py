"""Tests for is_favourite field, filter, and toggle endpoint."""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

INTERNAL_SECRET = "test-secret-xyz"


@pytest.fixture(autouse=True)
def _internal_secret_env(monkeypatch):
    # Pin the env var per-test so run order against other test modules that
    # overwrite it can't make our requests 401.
    monkeypatch.setenv("INTERNAL_API_SECRET", INTERNAL_SECRET)


from app.fastapi_app import create_fastapi_app
from app.schemas import MediaItemOut
from app.db import get_session

AUTH_HEADERS = {
    "X-Internal-Secret": INTERNAL_SECRET,
    "X-User-ID": "user-1",
}


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_media_out(id: str, is_favourite: bool = False) -> MediaItemOut:
    now = datetime.now(timezone.utc)
    return MediaItemOut(
        id=id,
        clip_id=None,
        type="image",
        prompt="test prompt",
        file_url="https://example.com/img.png",
        metadata={},
        output_spec=None,
        is_favourite=is_favourite,
        created_at=now,
        updated_at=now,
    )


async def _mock_session():
    """Dependency override: yields a MagicMock AsyncSession."""
    yield MagicMock()


# ── schema unit tests ─────────────────────────────────────────────────────────

class TestMediaItemOutSchema:
    def test_is_favourite_defaults_false(self):
        item = _make_media_out("abc")
        assert item.is_favourite is False

    def test_is_favourite_true(self):
        item = _make_media_out("abc", is_favourite=True)
        assert item.is_favourite is True

    def test_from_orm_row_maps_is_favourite(self):
        row = MagicMock()
        row.id = str(uuid.uuid4())
        row.clip_id = None
        row.type = "image"
        row.prompt = "p"
        row.file_url = "https://example.com/x.png"
        row.metadata_ = {}
        row.output_spec = None
        row.is_favourite = True
        row.name = "test-media"
        row.pipeline_run_id = None
        row.scene_id = None
        row.parent_media_id = None
        row.role = None
        row.created_at = datetime.now(timezone.utc)
        row.updated_at = datetime.now(timezone.utc)

        result = MediaItemOut.from_orm_row(row)
        assert result.is_favourite is True

    def test_from_orm_row_handles_none_is_favourite(self):
        """DB NULL is coerced to False."""
        row = MagicMock()
        row.id = str(uuid.uuid4())
        row.clip_id = None
        row.type = "image"
        row.prompt = "p"
        row.file_url = "https://example.com/x.png"
        row.metadata_ = {}
        row.output_spec = None
        row.is_favourite = None
        row.name = "test-media"
        row.pipeline_run_id = None
        row.scene_id = None
        row.parent_media_id = None
        row.role = None
        row.created_at = datetime.now(timezone.utc)
        row.updated_at = datetime.now(timezone.utc)

        result = MediaItemOut.from_orm_row(row)
        assert result.is_favourite is False


# ── API endpoint tests (mocked store) ────────────────────────────────────────

@pytest.fixture()
def client():
    app = create_fastapi_app()
    # Override the DB dependency so tests don't need a real Postgres
    app.dependency_overrides[get_session] = _mock_session
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


class TestToggleFavouriteEndpoint:
    def test_toggle_favourite_on(self, client):
        media_id = str(uuid.uuid4())
        expected = _make_media_out(media_id, is_favourite=True)

        with patch("app.stores.media.toggle_favourite", new=AsyncMock(return_value=expected)):
            resp = client.patch(
                f"/v1/media/{media_id}/favourite",
                json={"is_favourite": True},
                headers=AUTH_HEADERS,
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["is_favourite"] is True
        assert data["id"] == media_id

    def test_toggle_favourite_off(self, client):
        media_id = str(uuid.uuid4())
        expected = _make_media_out(media_id, is_favourite=False)

        with patch("app.stores.media.toggle_favourite", new=AsyncMock(return_value=expected)):
            resp = client.patch(
                f"/v1/media/{media_id}/favourite",
                json={"is_favourite": False},
                headers=AUTH_HEADERS,
            )

        assert resp.status_code == 200
        assert resp.json()["is_favourite"] is False

    def test_toggle_favourite_not_found(self, client):
        media_id = str(uuid.uuid4())

        with patch("app.stores.media.toggle_favourite", new=AsyncMock(return_value=None)):
            resp = client.patch(
                f"/v1/media/{media_id}/favourite",
                json={"is_favourite": True},
                headers=AUTH_HEADERS,
            )

        assert resp.status_code == 404

    def test_list_media_is_favourite_filter_passed(self, client):
        """Verify the is_favourite query param is accepted and forwarded."""
        from app.schemas import PagedResponse

        unfav_item = _make_media_out(str(uuid.uuid4()), is_favourite=False)
        paged = PagedResponse(items=[unfav_item], total=1, page=1, limit=50)

        with patch("app.stores.media.list_media", new=AsyncMock(return_value=paged)) as mock_list:
            resp = client.get(
                "/v1/media",
                params={"is_favourite": "false"},
                headers=AUTH_HEADERS,
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["is_favourite"] is False
