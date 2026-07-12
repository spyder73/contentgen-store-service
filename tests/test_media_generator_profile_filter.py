"""Tests for generator_profile_id filter in list_media.

The generator_profile_id is stored in JSONB metadata and used to filter
media items by the generator profile that created them.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(element, compiler, **kw):  # pragma: no cover - trivial
    return "JSON"


@compiles(UUID, "sqlite")
def _uuid_sqlite(element, compiler, **kw):  # pragma: no cover - trivial
    return "VARCHAR(36)"


from app.models import MediaItem  # noqa: E402  (after compiler registration)
from app.stores import media as media_store  # noqa: E402


async def _make_engine_factory():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: MediaItem.__table__.create(c))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    return engine, factory


async def _seed(factory, rows: list[dict]):
    async with factory() as s:
        for r in rows:
            s.add(MediaItem(**r))
        await s.commit()


def test_generator_profile_id_filter_returns_only_matches():
    """Filter by generator_profile_id returns only items with that profile."""

    async def run():
        uid = str(uuid.uuid4())
        engine, factory = await _make_engine_factory()
        try:
            base = datetime(2026, 6, 13, 10, 0, 0, tzinfo=timezone.utc)
            profiles = ["profile_a", "profile_b", "profile_c"]
            rows = []
            for i, profile in enumerate(profiles):
                rows.append(
                    dict(
                        id=str(uuid.uuid4()),
                        user_id=uid,
                        type="image",
                        file_url=f"/m/{i}.png",
                        metadata_={"generator_profile_id": profile},
                        created_at=base,
                    )
                )
            # Add row without generator_profile_id metadata
            rows.append(
                dict(
                    id=str(uuid.uuid4()),
                    user_id=uid,
                    type="image",
                    file_url="/m/no_profile.png",
                    metadata_={},
                    created_at=base,
                )
            )
            await _seed(factory, rows)

            async with factory() as s:
                # Filter for profile_a
                result_a = await media_store.list_media(
                    s, user_id=uid, generator_profile_id="profile_a", page=1, limit=50
                )
                # Filter for profile_b
                result_b = await media_store.list_media(
                    s, user_id=uid, generator_profile_id="profile_b", page=1, limit=50
                )
                # Filter for profile_c
                result_c = await media_store.list_media(
                    s, user_id=uid, generator_profile_id="profile_c", page=1, limit=50
                )
                # No filter — all rows
                result_all = await media_store.list_media(
                    s, user_id=uid, page=1, limit=50
                )

            # Each profile should return exactly one row
            assert result_a.total == 1
            assert result_a.items[0].metadata["generator_profile_id"] == "profile_a"

            assert result_b.total == 1
            assert result_b.items[0].metadata["generator_profile_id"] == "profile_b"

            assert result_c.total == 1
            assert result_c.items[0].metadata["generator_profile_id"] == "profile_c"

            # All rows without filter
            assert result_all.total == 4

        finally:
            await engine.dispose()

    asyncio.run(run())


def test_generator_profile_id_filter_with_source_filter():
    """Combine generator_profile_id filter with source filter."""

    async def run():
        uid = str(uuid.uuid4())
        engine, factory = await _make_engine_factory()
        try:
            base = datetime(2026, 6, 13, 10, 0, 0, tzinfo=timezone.utc)
            rows = [
                dict(
                    id=str(uuid.uuid4()),
                    user_id=uid,
                    type="image",
                    file_url="/m/1.png",
                    metadata_={
                        "generator_profile_id": "profile_gen",
                        "source": "generator_profile_test",
                    },
                    created_at=base,
                ),
                dict(
                    id=str(uuid.uuid4()),
                    user_id=uid,
                    type="image",
                    file_url="/m/2.png",
                    metadata_={
                        "generator_profile_id": "profile_gen",
                        "source": "manual_upload",
                    },
                    created_at=base,
                ),
                dict(
                    id=str(uuid.uuid4()),
                    user_id=uid,
                    type="image",
                    file_url="/m/3.png",
                    metadata_={
                        "generator_profile_id": "other_profile",
                        "source": "generator_profile_test",
                    },
                    created_at=base,
                ),
            ]
            await _seed(factory, rows)

            async with factory() as s:
                # Filter by both generator_profile_id and source
                result = await media_store.list_media(
                    s,
                    user_id=uid,
                    generator_profile_id="profile_gen",
                    source="generator_profile_test",
                    page=1,
                    limit=50,
                )

            # Should return exactly one row (profile_gen + generator_profile_test)
            assert result.total == 1
            assert result.items[0].metadata["generator_profile_id"] == "profile_gen"
            assert result.items[0].metadata["source"] == "generator_profile_test"

        finally:
            await engine.dispose()

    asyncio.run(run())


def test_generator_profile_id_filter_excludes_missing_metadata():
    """Rows without the generator_profile_id metadata key are excluded."""

    async def run():
        uid = str(uuid.uuid4())
        engine, factory = await _make_engine_factory()
        try:
            base = datetime(2026, 6, 13, 10, 0, 0, tzinfo=timezone.utc)
            rows = [
                dict(
                    id=str(uuid.uuid4()),
                    user_id=uid,
                    type="image",
                    file_url="/m/with_profile.png",
                    metadata_={"generator_profile_id": "target_profile"},
                    created_at=base,
                ),
                dict(
                    id=str(uuid.uuid4()),
                    user_id=uid,
                    type="image",
                    file_url="/m/without_profile.png",
                    metadata_={},
                    created_at=base,
                ),
                dict(
                    id=str(uuid.uuid4()),
                    user_id=uid,
                    type="image",
                    file_url="/m/other_profile.png",
                    metadata_={"generator_profile_id": "other_profile"},
                    created_at=base,
                ),
            ]
            await _seed(factory, rows)

            async with factory() as s:
                result = await media_store.list_media(
                    s,
                    user_id=uid,
                    generator_profile_id="target_profile",
                    page=1,
                    limit=50,
                )

            # Should return only the row with target_profile
            assert result.total == 1
            assert result.items[0].metadata["generator_profile_id"] == "target_profile"

        finally:
            await engine.dispose()

    asyncio.run(run())
