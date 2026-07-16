"""User-scoped media isolation — verifies the /v1/media routes forward the
X-User-ID header into the store layer as `user_id` and that missing it yields
401. The store layer itself filters on user_id; these tests confirm the HTTP
route doesn't accidentally short-circuit that filter."""
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
from app.schemas import MediaItemOut, PagedResponse


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


def _media_out(id_: str, user_id: str) -> MediaItemOut:
    now = datetime.now(timezone.utc)
    return MediaItemOut(
        id=id_,
        clip_id=None,
        type="image",
        prompt="p",
        file_url="/media/uploads/x.png",
        metadata={},
        output_spec=None,
        is_favourite=False,
        created_at=now,
        updated_at=now,
    )


class TestListForwardsUserId:
    def test_list_includes_user_id(self, client):
        uid = str(uuid.uuid4())
        paged = PagedResponse(items=[], total=0, page=1, limit=50)
        with patch("app.stores.media.list_media", new=AsyncMock(return_value=paged)) as m:
            resp = client.get("/v1/media", headers=_headers(uid))
        assert resp.status_code == 200
        assert m.call_args.kwargs.get("user_id") == uid

    def test_list_without_user_id_returns_401(self, client):
        resp = client.get("/v1/media", headers={"X-Internal-Secret": INTERNAL_SECRET})
        assert resp.status_code == 401

    def test_list_forwards_query_upgrades(self, client):
        uid = str(uuid.uuid4())
        paged = PagedResponse(items=[], total=0, page=2, limit=100)
        with patch("app.stores.media.list_media", new=AsyncMock(return_value=paged)) as m:
            resp = client.get(
                "/v1/media",
                params={
                    "sort": "name",
                    "created_after": "2026-07-10T00:00:00Z",
                    "has_controlnet": "true",
                    "page": 2,
                    "limit": 100,
                },
                headers=_headers(uid),
            )
        assert resp.status_code == 200
        kwargs = m.call_args.kwargs
        assert kwargs["sort"] == "name"
        assert kwargs["created_after"] == datetime(2026, 7, 10, tzinfo=timezone.utc)
        assert kwargs["has_controlnet"] is True
        assert kwargs["page"] == 2
        assert kwargs["limit"] == 100


class TestGetForwardsUserId:
    def test_get_includes_user_id(self, client):
        uid = str(uuid.uuid4())
        mid = str(uuid.uuid4())
        with patch(
            "app.stores.media.get_media",
            new=AsyncMock(return_value=_media_out(mid, uid)),
        ) as m:
            resp = client.get(f"/v1/media/{mid}", headers=_headers(uid))
        assert resp.status_code == 200
        assert m.call_args.kwargs.get("user_id") == uid

    def test_get_returns_404_for_other_users_item(self, client):
        # Store returns None for cross-user access; route surfaces 404.
        uid = str(uuid.uuid4())
        mid = str(uuid.uuid4())
        with patch("app.stores.media.get_media", new=AsyncMock(return_value=None)):
            resp = client.get(f"/v1/media/{mid}", headers=_headers(uid))
        assert resp.status_code == 404


class TestPatchForwardsUserId:
    def test_patch_includes_user_id_and_targeted_fields(self, client):
        uid = str(uuid.uuid4())
        mid = str(uuid.uuid4())
        out = _media_out(mid, uid)
        with patch("app.stores.media.patch_media", new=AsyncMock(return_value=out)) as mocked:
            resp = client.patch(
                f"/v1/media/{mid}",
                headers=_headers(uid),
                json={
                    "file_url": f"/media/uploads/{mid}.png",
                    "metadata_merge": {"persistence_status": "ready"},
                },
            )

        assert resp.status_code == 200
        assert mocked.call_args.kwargs["user_id"] == uid
        body = mocked.call_args.args[2]
        assert body.file_url == f"/media/uploads/{mid}.png"
        assert body.metadata_merge == {"persistence_status": "ready"}

    def test_patch_returns_404_when_not_owned(self, client):
        uid = str(uuid.uuid4())
        mid = str(uuid.uuid4())
        with patch("app.stores.media.patch_media", new=AsyncMock(return_value=None)):
            resp = client.patch(
                f"/v1/media/{mid}",
                headers=_headers(uid),
                json={"metadata_merge": {"persistence_status": "ready"}},
            )
        assert resp.status_code == 404

    def test_patch_requires_user_id(self, client):
        mid = str(uuid.uuid4())
        resp = client.patch(
            f"/v1/media/{mid}",
            headers={"X-Internal-Secret": INTERNAL_SECRET},
            json={"metadata_merge": {"persistence_status": "ready"}},
        )
        assert resp.status_code == 401


class TestDeleteForwardsUserId:
    def test_delete_includes_user_id(self, client):
        uid = str(uuid.uuid4())
        mid = str(uuid.uuid4())
        with patch("app.stores.media.delete_media", new=AsyncMock(return_value=True)) as m:
            resp = client.delete(f"/v1/media/{mid}", headers=_headers(uid))
        assert resp.status_code == 204
        assert m.call_args.kwargs.get("user_id") == uid

    def test_delete_returns_404_when_not_owned(self, client):
        uid = str(uuid.uuid4())
        mid = str(uuid.uuid4())
        with patch("app.stores.media.delete_media", new=AsyncMock(return_value=False)):
            resp = client.delete(f"/v1/media/{mid}", headers=_headers(uid))
        assert resp.status_code == 404


class TestFavouriteForwardsUserId:
    def test_favourite_includes_user_id(self, client):
        uid = str(uuid.uuid4())
        mid = str(uuid.uuid4())
        out = _media_out(mid, uid)
        with patch(
            "app.stores.media.toggle_favourite",
            new=AsyncMock(return_value=out),
        ) as m:
            resp = client.patch(
                f"/v1/media/{mid}/favourite",
                headers=_headers(uid),
                json={"is_favourite": True},
            )
        assert resp.status_code == 200
        assert m.call_args.kwargs.get("user_id") == uid

    def test_favourite_returns_404_when_not_owned(self, client):
        uid = str(uuid.uuid4())
        mid = str(uuid.uuid4())
        with patch("app.stores.media.toggle_favourite", new=AsyncMock(return_value=None)):
            resp = client.patch(
                f"/v1/media/{mid}/favourite",
                headers=_headers(uid),
                json={"is_favourite": True},
            )
        assert resp.status_code == 404
