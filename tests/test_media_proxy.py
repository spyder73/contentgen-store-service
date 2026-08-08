"""Tests for the media 480p proxy derivative persistence (video-editor D5).

Mirrors `test_media_thumbnail.py`'s structure:
* store-level roundtrip against in-memory sqlite (`store_proxy_data` then
  `get_proxy`),
* ownership scoping (`_get_by_id` semantics — optional user_id, still
  enforced when supplied),
* the `PUT`/`GET /v1/media/{id}/proxy` endpoints (mocked store) roundtrip and
  the `no_proxy` 404 when nothing has been generated yet.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles


# ── make the Postgres-only types renderable on sqlite (test DB only) ──────────
@compiles(JSONB, "sqlite")
def _jsonb_sqlite(element, compiler, **kw):  # pragma: no cover - trivial
    return "JSON"


@compiles(UUID, "sqlite")
def _uuid_sqlite(element, compiler, **kw):  # pragma: no cover - trivial
    return "VARCHAR(36)"


from app.models import MediaItem  # noqa: E402  (after compiler registration)
from app.stores import media as media_store  # noqa: E402

INTERNAL_SECRET = "test-secret-proxy"


async def _make_factory():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: MediaItem.__table__.create(c))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    return engine, factory


async def _seed_video_row(factory, user_id: str) -> str:
    media_id = str(uuid.uuid4())
    async with factory() as s:
        s.add(
            MediaItem(
                id=media_id,
                user_id=user_id,
                type="video",
                prompt="p",
                file_url="/media/x.mp4",
                metadata_={},
                name="vid",
                created_at=datetime.now(timezone.utc),
            )
        )
        await s.commit()
    return media_id


# ── store: roundtrip ───────────────────────────────────────────────────────────

def test_store_proxy_data_then_get_proxy_roundtrips():
    async def run():
        uid = str(uuid.uuid4())
        engine, factory = await _make_factory()
        try:
            mid = await _seed_video_row(factory, uid)
            proxy_bytes = b"fake-480p-mp4-bytes"
            async with factory() as s:
                ok = await media_store.store_proxy_data(
                    s, mid, proxy_bytes, "video/mp4", user_id=uid
                )
            assert ok is True

            async with factory() as s:
                result = await media_store.get_proxy(s, mid, user_id=uid)
            assert result == (proxy_bytes, "video/mp4")
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_get_proxy_returns_none_without_proxy():
    async def run():
        uid = str(uuid.uuid4())
        engine, factory = await _make_factory()
        try:
            mid = await _seed_video_row(factory, uid)
            async with factory() as s:
                result = await media_store.get_proxy(s, mid, user_id=uid)
            assert result is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_store_proxy_data_returns_false_for_missing_row():
    async def run():
        engine, factory = await _make_factory()
        try:
            async with factory() as s:
                ok = await media_store.store_proxy_data(
                    s, str(uuid.uuid4()), b"bytes", "video/mp4", user_id=str(uuid.uuid4())
                )
            assert ok is False
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_get_proxy_enforces_ownership_when_user_id_supplied():
    async def run():
        owner = str(uuid.uuid4())
        intruder = str(uuid.uuid4())
        engine, factory = await _make_factory()
        try:
            mid = await _seed_video_row(factory, owner)
            async with factory() as s:
                await media_store.store_proxy_data(
                    s, mid, b"bytes", "video/mp4", user_id=owner
                )
            async with factory() as s:
                result = await media_store.get_proxy(s, mid, user_id=intruder)
            assert result is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_get_proxy_allows_missing_user_id_like_thumbnail():
    # Mirrors get_thumbnail's optional-ownership semantics: the byte-serving
    # accessor is reached via the go-backend's unauthenticated embed path with
    # no X-User-ID, so user_id=None must not be treated as "no access".
    async def run():
        owner = str(uuid.uuid4())
        engine, factory = await _make_factory()
        try:
            mid = await _seed_video_row(factory, owner)
            async with factory() as s:
                await media_store.store_proxy_data(
                    s, mid, b"bytes", "video/mp4", user_id=owner
                )
            async with factory() as s:
                result = await media_store.get_proxy(s, mid, user_id=None)
            assert result == (b"bytes", "video/mp4")
        finally:
            await engine.dispose()

    asyncio.run(run())


# ── endpoint: PUT/GET /v1/media/{id}/proxy (mocked store) ──────────────────────

@pytest.fixture(autouse=True)
def _internal_secret_env(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_SECRET", INTERNAL_SECRET)


@pytest.fixture()
def client():
    from app.db import get_session
    from app.fastapi_app import create_fastapi_app
    from fastapi.testclient import TestClient

    async def _mock_session():
        yield MagicMock()

    app = create_fastapi_app()
    app.dependency_overrides[get_session] = _mock_session
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


_AUTH = {"X-Internal-Secret": INTERNAL_SECRET, "X-User-ID": "user-proxy"}
_AUTH_NO_USER = {"X-Internal-Secret": INTERNAL_SECRET}


def test_proxy_endpoint_put_then_get_roundtrips(client):
    mid = str(uuid.uuid4())
    body = b"proxy-mp4-bytes"

    with patch("app.stores.media.store_proxy_data", new=AsyncMock(return_value=True)) as mocked_put:
        put_resp = client.put(
            f"/v1/media/{mid}/proxy",
            content=body,
            headers={**_AUTH, "Content-Type": "video/mp4"},
        )
    assert put_resp.status_code == 204
    mocked_put.assert_awaited_once()
    assert mocked_put.await_args.args[1] == mid
    assert mocked_put.await_args.args[2] == body
    assert mocked_put.await_args.args[3] == "video/mp4"

    with patch(
        "app.stores.media.get_proxy", new=AsyncMock(return_value=(body, "video/mp4"))
    ):
        get_resp = client.get(f"/v1/media/{mid}/proxy", headers=_AUTH)
    assert get_resp.status_code == 200
    assert get_resp.headers["content-type"] == "video/mp4"
    assert get_resp.content == body
    assert "max-age" in get_resp.headers.get("cache-control", "")


def test_proxy_endpoint_get_404_when_no_proxy(client):
    mid = str(uuid.uuid4())
    with patch("app.stores.media.get_proxy", new=AsyncMock(return_value=None)):
        resp = client.get(f"/v1/media/{mid}/proxy", headers=_AUTH)
    assert resp.status_code == 404
    assert resp.json()["detail"] == "no_proxy"


def test_proxy_endpoint_put_404_when_media_missing(client):
    mid = str(uuid.uuid4())
    with patch("app.stores.media.store_proxy_data", new=AsyncMock(return_value=False)):
        resp = client.put(
            f"/v1/media/{mid}/proxy",
            content=b"bytes",
            headers={**_AUTH, "Content-Type": "video/mp4"},
        )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "not_found"


def test_proxy_endpoint_put_400_when_body_empty(client):
    mid = str(uuid.uuid4())
    resp = client.put(f"/v1/media/{mid}/proxy", content=b"", headers=_AUTH)
    assert resp.status_code == 400


def test_proxy_endpoint_accepts_internal_secret_without_user_id(client):
    # Mirrors test_media_byte_routes_internal_auth.py: the Go backend's
    # public embed / background-fill paths forward X-Internal-Secret but not
    # X-User-ID.
    mid = str(uuid.uuid4())
    body = b"proxy-bytes"
    with patch(
        "app.stores.media.get_proxy", new=AsyncMock(return_value=(body, "video/mp4"))
    ):
        resp = client.get(f"/v1/media/{mid}/proxy", headers=_AUTH_NO_USER)
    assert resp.status_code == 200
    assert resp.content == body
