"""Tests for the media thumbnail derivative pipeline.

Covers:
* eager thumbnail generation when image bytes are stored (`store_file_data`),
* lazy backfill on the first `get_thumbnail` for a legacy row that has bytes but
  no derivative yet (then served from the persisted column on the next call),
* `has_thumbnail` surfacing in the list/schema so the Go handler can advertise a
  thumbnail URL only when one resolves,
* the `GET /v1/media/{id}/thumbnail` endpoint (mocked store) returning the
  derivative or 404 (fall back to original).

DB-integration tests run against in-memory sqlite with lightweight renderings of
the Postgres-only column types, mirroring `test_media_list_deferred_blob.py`.
These are skipped when Pillow is unavailable (no derivative can be produced).
"""
from __future__ import annotations

import asyncio
import io
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from app import derivatives


# ── make the Postgres-only types renderable on sqlite (test DB only) ──────────
@compiles(JSONB, "sqlite")
def _jsonb_sqlite(element, compiler, **kw):  # pragma: no cover - trivial
    return "JSON"


@compiles(UUID, "sqlite")
def _uuid_sqlite(element, compiler, **kw):  # pragma: no cover - trivial
    return "VARCHAR(36)"


from app.models import MediaItem  # noqa: E402  (after compiler registration)
from app.schemas import MediaItemOut, PagedResponse  # noqa: E402
from app.stores import media as media_store  # noqa: E402

INTERNAL_SECRET = "test-secret-thumb"

_PIL = derivatives._PIL_AVAILABLE


def _png_bytes(width: int, height: int) -> bytes:
    from PIL import Image

    img = Image.new("RGB", (width, height), (40, 90, 160))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


async def _make_factory():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: MediaItem.__table__.create(c))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    return engine, factory


async def _seed_image_row(factory, user_id: str, *, with_bytes: bytes | None) -> str:
    media_id = str(uuid.uuid4())
    async with factory() as s:
        s.add(
            MediaItem(
                id=media_id,
                user_id=user_id,
                type="image",
                prompt="p",
                file_url="/media/x.png",
                metadata_={},
                name="img",
                file_data=with_bytes,
                file_mime_type="image/png" if with_bytes else None,
                created_at=datetime.now(timezone.utc),
            )
        )
        await s.commit()
    return media_id


# ── store: eager generation on store_file_data ────────────────────────────────

@pytest.mark.skipif(not _PIL, reason="Pillow not installed")
def test_store_file_data_generates_thumbnail_for_image():
    async def run():
        uid = str(uuid.uuid4())
        engine, factory = await _make_factory()
        try:
            mid = await _seed_image_row(factory, uid, with_bytes=None)
            png = _png_bytes(1200, 900)
            async with factory() as s:
                ok = await media_store.store_file_data(s, mid, png, "image/png", user_id=uid)
            assert ok is True
            async with factory() as s:
                row = await s.get(MediaItem, mid)
                assert row.thumbnail_data is not None
                assert row.thumbnail_content_type == derivatives.THUMBNAIL_CONTENT_TYPE
                assert len(row.thumbnail_data) < len(png)
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_store_file_data_skips_thumbnail_for_non_image():
    async def run():
        uid = str(uuid.uuid4())
        engine, factory = await _make_factory()
        try:
            media_id = str(uuid.uuid4())
            async with factory() as s:
                s.add(
                    MediaItem(
                        id=media_id,
                        user_id=uid,
                        type="video",
                        prompt="p",
                        file_url="/media/x.mp4",
                        metadata_={},
                        name="vid",
                        created_at=datetime.now(timezone.utc),
                    )
                )
                await s.commit()
            async with factory() as s:
                ok = await media_store.store_file_data(
                    s, media_id, b"\x00\x01\x02fakevideo", "video/mp4", user_id=uid
                )
            assert ok is True
            async with factory() as s:
                row = await s.get(MediaItem, media_id)
                assert row.thumbnail_data is None
                assert row.thumbnail_content_type is None
        finally:
            await engine.dispose()

    asyncio.run(run())


# ── store: lazy backfill on first get_thumbnail ───────────────────────────────

@pytest.mark.skipif(not _PIL, reason="Pillow not installed")
def test_get_thumbnail_lazy_backfills_legacy_row():
    async def run():
        uid = str(uuid.uuid4())
        engine, factory = await _make_factory()
        try:
            png = _png_bytes(1000, 1000)
            # Legacy row: has original bytes but NO derivative.
            mid = await _seed_image_row(factory, uid, with_bytes=png)
            async with factory() as s:
                row = await s.get(MediaItem, mid)
                assert row.thumbnail_data is None  # pre-condition

            # First GET backfills.
            async with factory() as s:
                result = await media_store.get_thumbnail(s, mid, user_id=uid)
            assert result is not None
            data, content_type = result
            assert content_type == derivatives.THUMBNAIL_CONTENT_TYPE

            # It is now persisted (served from the column, not regenerated).
            async with factory() as s:
                row = await s.get(MediaItem, mid)
                assert row.thumbnail_data is not None
                assert row.thumbnail_content_type == derivatives.THUMBNAIL_CONTENT_TYPE
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_get_thumbnail_returns_none_without_bytes():
    async def run():
        uid = str(uuid.uuid4())
        engine, factory = await _make_factory()
        try:
            mid = await _seed_image_row(factory, uid, with_bytes=None)
            async with factory() as s:
                result = await media_store.get_thumbnail(s, mid, user_id=uid)
            assert result is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_get_thumbnail_enforces_ownership():
    async def run():
        owner = str(uuid.uuid4())
        intruder = str(uuid.uuid4())
        engine, factory = await _make_factory()
        try:
            mid = await _seed_image_row(factory, owner, with_bytes=None)
            async with factory() as s:
                result = await media_store.get_thumbnail(s, mid, user_id=intruder)
            assert result is None
        finally:
            await engine.dispose()

    asyncio.run(run())


# ── has_thumbnail surfacing ───────────────────────────────────────────────────

def test_list_media_surfaces_has_thumbnail_for_image_with_bytes():
    async def run():
        uid = str(uuid.uuid4())
        engine, factory = await _make_factory()
        try:
            await _seed_image_row(factory, uid, with_bytes=b"\x89PNGfake")
            async with factory() as s:
                resp = await media_store.list_media(s, user_id=uid, page=1, limit=50)
            assert resp.total == 1
            assert resp.items[0].has_thumbnail is True
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_list_media_has_thumbnail_false_for_image_without_bytes():
    async def run():
        uid = str(uuid.uuid4())
        engine, factory = await _make_factory()
        try:
            await _seed_image_row(factory, uid, with_bytes=None)
            async with factory() as s:
                resp = await media_store.list_media(s, user_id=uid, page=1, limit=50)
            assert resp.items[0].has_thumbnail is False
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_media_item_out_has_thumbnail_from_thumbnail_content_type():
    row = MagicMock()
    row.id = str(uuid.uuid4())
    row.clip_id = None
    row.type = "video"  # not an image, but a derivative was stored
    row.prompt = "p"
    row.file_url = "/media/x.mp4"
    row.metadata_ = {}
    row.output_spec = None
    row.is_favourite = False
    row.name = "v"
    row.pipeline_run_id = None
    row.scene_id = None
    row.parent_media_id = None
    row.role = None
    row.thumbnail_content_type = "image/webp"
    row.file_mime_type = None
    row.created_at = datetime.now(timezone.utc)
    row.updated_at = datetime.now(timezone.utc)
    out = MediaItemOut.from_orm_row(row)
    assert out.has_thumbnail is True


# ── endpoint: GET /v1/media/{id}/thumbnail (mocked store) ──────────────────────

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


_AUTH = {"X-Internal-Secret": INTERNAL_SECRET, "X-User-ID": "user-thumb"}


def test_thumbnail_endpoint_returns_derivative(client):
    mid = str(uuid.uuid4())
    payload = (b"webp-bytes", "image/webp")
    with patch("app.stores.media.get_thumbnail", new=AsyncMock(return_value=payload)):
        resp = client.get(f"/v1/media/{mid}/thumbnail", headers=_AUTH)
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/webp"
    assert resp.content == b"webp-bytes"
    assert "max-age" in resp.headers.get("cache-control", "")


def test_thumbnail_endpoint_404_when_no_derivative(client):
    mid = str(uuid.uuid4())
    with patch("app.stores.media.get_thumbnail", new=AsyncMock(return_value=None)):
        resp = client.get(f"/v1/media/{mid}/thumbnail", headers=_AUTH)
    assert resp.status_code == 404
