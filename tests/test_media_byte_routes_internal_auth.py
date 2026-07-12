"""Audit finding #12 (store side): the media byte-serving routes —
GET /v1/media/{id}/file and GET /v1/media/{id}/thumbnail — must accept a
request authenticated by the X-Internal-Secret gate even when X-User-ID is
absent.

Why: these routes are reached via the go-backend's public, unauthenticated
/media/uploads embed path. The go-backend forwards X-Internal-Secret but
never X-User-ID for that path, so after a redeploy the store-restore
fallback previously 401'd here (see media.get_file_data / get_thumbnail
requiring a truthy user_id), which the go-backend surfaced to the browser as
a 404. The UUID id is the access control for byte serving on these two
routes specifically — not the user id — since a caller reaching them at all
already cleared the internal-secret gate.

X-User-ID, when supplied, must still be honored as an extra ownership
filter (cross-user access continues to 404), preserving existing scoped
behavior for authenticated store callers.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

INTERNAL_SECRET = "test-secret-byte-routes"


@pytest.fixture(autouse=True)
def _internal_secret_env(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_SECRET", INTERNAL_SECRET)


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


INTERNAL_ONLY_HEADERS = {"X-Internal-Secret": INTERNAL_SECRET}


class TestFileRouteAcceptsInternalSecretWithoutUserId:
    def test_returns_200_and_bytes_without_x_user_id(self, client):
        mid = str(uuid.uuid4())
        payload = (b"original-bytes", "image/png")
        with patch(
            "app.stores.media.get_file_data", new=AsyncMock(return_value=payload)
        ) as mocked:
            resp = client.get(f"/v1/media/{mid}/file", headers=INTERNAL_ONLY_HEADERS)
        assert resp.status_code == 200
        assert resp.content == b"original-bytes"
        assert resp.headers["content-type"] == "image/png"
        # No X-User-ID present -> the store is called with user_id=None, not
        # short-circuited by a 401 before ever reaching the store layer.
        assert mocked.call_args.kwargs.get("user_id") is None

    def test_still_forwards_x_user_id_when_present(self, client):
        mid = str(uuid.uuid4())
        uid = str(uuid.uuid4())
        payload = (b"original-bytes", "image/png")
        with patch(
            "app.stores.media.get_file_data", new=AsyncMock(return_value=payload)
        ) as mocked:
            resp = client.get(
                f"/v1/media/{mid}/file",
                headers={**INTERNAL_ONLY_HEADERS, "X-User-ID": uid},
            )
        assert resp.status_code == 200
        assert mocked.call_args.kwargs.get("user_id") == uid

    def test_404_when_no_file_data(self, client):
        mid = str(uuid.uuid4())
        with patch("app.stores.media.get_file_data", new=AsyncMock(return_value=None)):
            resp = client.get(f"/v1/media/{mid}/file", headers=INTERNAL_ONLY_HEADERS)
        assert resp.status_code == 404

    def test_401_when_internal_secret_missing(self, client):
        resp = client.get(f"/v1/media/{uuid.uuid4()}/file")
        assert resp.status_code == 401


class TestThumbnailRouteAcceptsInternalSecretWithoutUserId:
    def test_returns_200_and_bytes_without_x_user_id(self, client):
        mid = str(uuid.uuid4())
        payload = (b"thumb-bytes", "image/webp")
        with patch(
            "app.stores.media.get_thumbnail", new=AsyncMock(return_value=payload)
        ) as mocked:
            resp = client.get(f"/v1/media/{mid}/thumbnail", headers=INTERNAL_ONLY_HEADERS)
        assert resp.status_code == 200
        assert resp.content == b"thumb-bytes"
        assert resp.headers["content-type"] == "image/webp"
        assert mocked.call_args.kwargs.get("user_id") is None

    def test_still_forwards_x_user_id_when_present(self, client):
        mid = str(uuid.uuid4())
        uid = str(uuid.uuid4())
        payload = (b"thumb-bytes", "image/webp")
        with patch(
            "app.stores.media.get_thumbnail", new=AsyncMock(return_value=payload)
        ) as mocked:
            resp = client.get(
                f"/v1/media/{mid}/thumbnail",
                headers={**INTERNAL_ONLY_HEADERS, "X-User-ID": uid},
            )
        assert resp.status_code == 200
        assert mocked.call_args.kwargs.get("user_id") == uid

    def test_401_when_internal_secret_missing(self, client):
        resp = client.get(f"/v1/media/{uuid.uuid4()}/thumbnail")
        assert resp.status_code == 401


class TestGetFileDataStoreLayerHonorsOptionalUserId:
    """Unit-level coverage of app.stores.media.get_file_data / get_thumbnail
    against a real (sqlite) row, mirroring the ownership-filter tests in
    tests/test_media_thumbnail.py."""

    @staticmethod
    async def _make_factory():
        from sqlalchemy.dialects.postgresql import JSONB, UUID
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
        from sqlalchemy.ext.compiler import compiles

        @compiles(JSONB, "sqlite")
        def _jsonb_sqlite(element, compiler, **kw):  # pragma: no cover - trivial
            return "JSON"

        @compiles(UUID, "sqlite")
        def _uuid_sqlite(element, compiler, **kw):  # pragma: no cover - trivial
            return "VARCHAR(36)"

        from app.models import MediaItem

        engine = create_async_engine("sqlite+aiosqlite://")
        async with engine.begin() as conn:
            await conn.run_sync(lambda c: MediaItem.__table__.create(c))
        factory = async_sessionmaker(engine, expire_on_commit=False)
        return engine, factory

    def test_get_file_data_returns_bytes_without_user_id(self):
        import asyncio
        from datetime import datetime, timezone

        from app.models import MediaItem
        from app.stores import media as media_store

        async def run():
            engine, factory = await self._make_factory()
            try:
                mid = str(uuid.uuid4())
                owner = str(uuid.uuid4())
                async with factory() as s:
                    s.add(
                        MediaItem(
                            id=mid,
                            user_id=owner,
                            type="image",
                            prompt="p",
                            file_url="/media/x.png",
                            metadata_={},
                            name="img",
                            file_data=b"bytes-in-db",
                            file_mime_type="image/png",
                            created_at=datetime.now(timezone.utc),
                        )
                    )
                    await s.commit()
                async with factory() as s:
                    result = await media_store.get_file_data(s, mid, user_id=None)
                assert result == (b"bytes-in-db", "image/png")
            finally:
                await engine.dispose()

        asyncio.run(run())

    def test_get_file_data_still_enforces_ownership_when_user_id_given(self):
        import asyncio
        from datetime import datetime, timezone

        from app.models import MediaItem
        from app.stores import media as media_store

        async def run():
            engine, factory = await self._make_factory()
            try:
                mid = str(uuid.uuid4())
                owner = str(uuid.uuid4())
                intruder = str(uuid.uuid4())
                async with factory() as s:
                    s.add(
                        MediaItem(
                            id=mid,
                            user_id=owner,
                            type="image",
                            prompt="p",
                            file_url="/media/x.png",
                            metadata_={},
                            name="img",
                            file_data=b"bytes-in-db",
                            file_mime_type="image/png",
                            created_at=datetime.now(timezone.utc),
                        )
                    )
                    await s.commit()
                async with factory() as s:
                    result = await media_store.get_file_data(s, mid, user_id=intruder)
                assert result is None
            finally:
                await engine.dispose()

        asyncio.run(run())
