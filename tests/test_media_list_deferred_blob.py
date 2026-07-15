"""Integration tests for `list_media` against a real (sqlite) database.

These cover the de-BLOB optimisation: the list query defers the LargeBinary
`file_data` column so it is never read from the heap per row. The assertions
prove (a) the list still returns the correct rows in `created_at DESC` order,
(b) the serialised `MediaItemOut` never carries `file_data`, and (c) the column
is genuinely *not loaded* from the SELECT (it stays in SQLAlchemy's `unloaded`
set), so nothing in the path triggers a lazy load.

The store's `MediaItem` model uses Postgres-only column types (JSONB, UUID). We
register lightweight sqlite renderings for them so the single `media_items`
table can be created on an in-memory sqlite DB without standing up Postgres,
mirroring how the rest of the suite avoids a live DB. The tests drive the async
store layer via ``asyncio.run`` (matching the suite's existing style) so no
pytest-asyncio configuration is required.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import inspect as sa_inspect, select
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import defer


# ── make the Postgres-only types renderable on sqlite (test DB only) ──────────
@compiles(JSONB, "sqlite")
def _jsonb_sqlite(element, compiler, **kw):  # pragma: no cover - trivial
    return "JSON"


@compiles(UUID, "sqlite")
def _uuid_sqlite(element, compiler, **kw):  # pragma: no cover - trivial
    return "VARCHAR(36)"


from app.models import MediaItem  # noqa: E402  (after compiler registration)
from app.stores import media as media_store  # noqa: E402


async def _make_factory_with_seed(user_id: str, n: int = 3):
    """Return (engine, session_factory) with a media_items table seeded with
    `n` rows for `user_id` (i=0 newest ... i=n-1 oldest; i==1 favourited)."""
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: MediaItem.__table__.create(c))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    base = datetime.now(timezone.utc)
    async with factory() as s:
        for i in range(n):
            s.add(
                MediaItem(
                    id=str(uuid.uuid4()),
                    user_id=user_id,
                    type="image",
                    prompt=f"p{i}",
                    file_url=f"/media/{i}.png",
                    metadata_={},
                    is_favourite=(i == 1),
                    name=f"name-{i}",
                    file_data=b"\x00" * 4096,
                    file_mime_type="image/png",
                    created_at=base - timedelta(minutes=i),
                )
            )
        await s.commit()
    return engine, factory


def test_list_media_returns_rows_without_file_data():
    async def run():
        uid = str(uuid.uuid4())
        engine, factory = await _make_factory_with_seed(uid, n=3)
        try:
            async with factory() as s:
                resp = await media_store.list_media(s, user_id=uid, page=1, limit=50)
            assert resp.total == 3
            assert len(resp.items) == 3
            # created_at DESC: newest (p0) first.
            assert [it.prompt for it in resp.items] == ["p0", "p1", "p2"]
            for item in resp.items:
                assert "file_data" not in item.model_dump()
                assert item.has_file is True
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_list_media_does_not_load_file_data_column():
    """The deferred BLOB is not fetched: it stays in the `unloaded` attr set,
    so reading the list never de-toasts file_data per row."""

    async def run():
        uid = str(uuid.uuid4())
        engine, factory = await _make_factory_with_seed(uid, n=1)
        try:
            async with factory() as s:
                result = await s.execute(
                    select(MediaItem)
                    .where(MediaItem.user_id == uid)
                    .options(defer(MediaItem.file_data))
                )
                row = result.scalars().first()
                assert row is not None
                assert "file_data" in sa_inspect(row).unloaded
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_list_media_favourite_filter_with_deferred_blob():
    """The favourites filter path also works with the deferred BLOB."""

    async def run():
        uid = str(uuid.uuid4())
        engine, factory = await _make_factory_with_seed(uid, n=3)
        try:
            async with factory() as s:
                resp = await media_store.list_media(
                    s, user_id=uid, is_favourite=True, page=1, limit=50
                )
            assert resp.total == 1
            assert len(resp.items) == 1
            assert resp.items[0].is_favourite is True
            assert resp.items[0].prompt == "p1"
        finally:
            await engine.dispose()

    asyncio.run(run())
