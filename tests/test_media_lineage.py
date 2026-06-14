"""Tests for the media lineage query (`get_related_media`).

Lineage is derived entirely from already-persisted columns:
* ``parent``     — the row referenced by this item's ``parent_media_id``.
* ``variations`` — rows whose ``parent_media_id`` points back at this item.
* ``siblings``   — rows sharing this item's ``parent_media_id`` (excl. itself).

All scoped to the owning user. DB-integration against in-memory sqlite, mirroring
`test_media_thumbnail.py`.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

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


async def _make_factory():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: MediaItem.__table__.create(c))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    return engine, factory


async def _add(factory, *, mid, user_id, parent=None, when_offset=0, name="m"):
    async with factory() as s:
        s.add(
            MediaItem(
                id=mid,
                user_id=user_id,
                type="image",
                prompt="p",
                file_url=f"/media/{mid}.png",
                metadata_={},
                name=name,
                parent_media_id=parent,
                created_at=datetime.now(timezone.utc) + timedelta(seconds=when_offset),
            )
        )
        await s.commit()


def test_get_related_media_returns_parent_siblings_and_variations():
    async def run():
        uid = str(uuid.uuid4())
        engine, factory = await _make_factory()
        try:
            parent = str(uuid.uuid4())
            target = str(uuid.uuid4())
            sibling = str(uuid.uuid4())
            child_a = str(uuid.uuid4())
            child_b = str(uuid.uuid4())

            await _add(factory, mid=parent, user_id=uid, name="parent")
            await _add(factory, mid=target, user_id=uid, parent=parent, name="target")
            await _add(factory, mid=sibling, user_id=uid, parent=parent, name="sibling")
            await _add(factory, mid=child_a, user_id=uid, parent=target, name="child-a", when_offset=1)
            await _add(factory, mid=child_b, user_id=uid, parent=target, name="child-b", when_offset=2)

            async with factory() as s:
                related = await media_store.get_related_media(s, target, user_id=uid)

            assert related is not None
            assert related.parent is not None and related.parent.id == parent
            assert {it.id for it in related.siblings} == {sibling}
            assert {it.id for it in related.variations} == {child_a, child_b}
            # The target itself never appears in any list.
            all_ids = {related.parent.id} | {s.id for s in related.siblings} | {
                v.id for v in related.variations
            }
            assert target not in all_ids
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_get_related_media_empty_for_root_without_lineage():
    async def run():
        uid = str(uuid.uuid4())
        engine, factory = await _make_factory()
        try:
            mid = str(uuid.uuid4())
            await _add(factory, mid=mid, user_id=uid, name="lonely")
            async with factory() as s:
                related = await media_store.get_related_media(s, mid, user_id=uid)
            assert related is not None
            assert related.parent is None
            assert related.siblings == []
            assert related.variations == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_get_related_media_none_for_missing_item():
    async def run():
        uid = str(uuid.uuid4())
        engine, factory = await _make_factory()
        try:
            async with factory() as s:
                related = await media_store.get_related_media(s, str(uuid.uuid4()), user_id=uid)
            assert related is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_get_related_media_enforces_ownership():
    async def run():
        owner = str(uuid.uuid4())
        intruder = str(uuid.uuid4())
        engine, factory = await _make_factory()
        try:
            mid = str(uuid.uuid4())
            await _add(factory, mid=mid, user_id=owner, name="owned")
            async with factory() as s:
                related = await media_store.get_related_media(s, mid, user_id=intruder)
            assert related is None  # not owned → not found
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_get_related_media_excludes_other_users_variations():
    async def run():
        owner = str(uuid.uuid4())
        other = str(uuid.uuid4())
        engine, factory = await _make_factory()
        try:
            target = str(uuid.uuid4())
            mine = str(uuid.uuid4())
            theirs = str(uuid.uuid4())
            await _add(factory, mid=target, user_id=owner, name="target")
            await _add(factory, mid=mine, user_id=owner, parent=target, name="mine")
            # A variation owned by a different user must never leak in.
            await _add(factory, mid=theirs, user_id=other, parent=target, name="theirs")
            async with factory() as s:
                related = await media_store.get_related_media(s, target, user_id=owner)
            assert {it.id for it in related.variations} == {mine}
        finally:
            await engine.dispose()

    asyncio.run(run())
