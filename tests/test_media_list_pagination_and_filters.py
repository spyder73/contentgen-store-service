"""Tests for deterministic pagination and type/source filtering in `list_media`.

Two long-standing bugs are covered here:

* **Pagination duplicates** — when many rows share the *same* ``created_at`` (a
  batch of generated images written in one transaction), ``ORDER BY created_at
  DESC`` with no secondary key is non-deterministic under ``LIMIT/OFFSET``: the
  same row can resurface on a later page. The fix adds ``id`` as a stable
  tiebreaker, so paging over tied rows yields DISJOINT pages whose union is the
  whole set with no duplicates.

* **Source-bucket filter** — "uploaded" vs "generated" is not a single literal
  column value. Uploads carry ``metadata.source`` in
  {manual_upload, upload, upload_pool, uploaded}; everything else (generated,
  render_output, persisted, NULL) is "generated". The store must filter by these
  buckets, not an exact string match.

The store's ``MediaItem`` model uses Postgres-only column types (JSONB, UUID);
we register lightweight sqlite renderings so a single ``media_items`` table can
be created on an in-memory sqlite DB, matching the rest of the suite.
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


# ── Bug 5: deterministic pagination over duplicate created_at ─────────────────

def test_pages_are_disjoint_when_created_at_is_identical():
    """All rows share one created_at instant (a single-transaction batch). Paging
    in chunks must return DISJOINT pages whose union is the whole set — no row may
    appear on two pages, and none may be missing."""

    async def run():
        uid = str(uuid.uuid4())
        engine, factory = await _make_engine_factory()
        try:
            same_ts = datetime(2026, 6, 13, 10, 0, 0, tzinfo=timezone.utc)
            n = 50
            ids = [str(uuid.uuid4()) for _ in range(n)]
            await _seed(
                factory,
                [
                    dict(
                        id=ids[i],
                        user_id=uid,
                        type="image",
                        prompt=f"p{i}",
                        file_url=f"/media/{i}.png",
                        metadata_={},
                        created_at=same_ts,  # IDENTICAL timestamp for every row
                    )
                    for i in range(n)
                ],
            )

            seen: list[str] = []
            limit = 20
            async with factory() as s:
                page = 1
                while True:
                    resp = await media_store.list_media(
                        s, user_id=uid, page=page, limit=limit
                    )
                    if not resp.items:
                        break
                    seen.extend(it.id for it in resp.items)
                    page += 1
                    if page > 10:  # safety
                        break

            # No duplicates across pages, and every row appears exactly once.
            assert len(seen) == n, f"expected {n} rows across pages, got {len(seen)}"
            assert len(set(seen)) == n, "a row appeared on more than one page (duplicate)"
            assert set(seen) == set(ids)
            # With ties broken by a stable secondary key (id DESC), the paged
            # sequence must equal a single global sort by (created_at DESC, id DESC).
            # Asserting this proves the ORDER BY is deterministic (and not relying
            # on the storage engine's incidental row order).
            expected = sorted(ids, reverse=True)
            assert seen == expected, "paged order is not the deterministic (created_at, id) order"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_order_is_stable_across_repeated_calls_with_ties():
    """Two identical paged queries must return the rows in the same order even
    when created_at ties — proving the ORDER BY is fully deterministic."""

    async def run():
        uid = str(uuid.uuid4())
        engine, factory = await _make_engine_factory()
        try:
            same_ts = datetime(2026, 6, 13, 10, 0, 0, tzinfo=timezone.utc)
            ids = [str(uuid.uuid4()) for _ in range(30)]
            await _seed(
                factory,
                [
                    dict(
                        id=ids[i],
                        user_id=uid,
                        type="image",
                        file_url=f"/m/{i}.png",
                        metadata_={},
                        created_at=same_ts,
                    )
                    for i in range(30)
                ],
            )
            async with factory() as s:
                first = await media_store.list_media(s, user_id=uid, page=2, limit=10)
                second = await media_store.list_media(s, user_id=uid, page=2, limit=10)
            assert [it.id for it in first.items] == [it.id for it in second.items]
        finally:
            await engine.dispose()

    asyncio.run(run())


# ── Bug 6: source-bucket filter (uploaded vs generated) ───────────────────────

def test_source_filter_uploaded_returns_only_upload_sources():
    async def run():
        uid = str(uuid.uuid4())
        engine, factory = await _make_engine_factory()
        try:
            base = datetime(2026, 6, 13, 10, 0, 0, tzinfo=timezone.utc)
            specs = [
                ("manual_upload",),
                ("upload",),
                ("upload_pool",),
                ("uploaded",),
                ("generated",),
                ("render_output",),
                ("persisted",),
            ]
            await _seed(
                factory,
                [
                    dict(
                        id=str(uuid.uuid4()),
                        user_id=uid,
                        type="image",
                        file_url=f"/m/{i}.png",
                        metadata_={"source": s[0]},
                        created_at=base,
                    )
                    for i, s in enumerate(specs)
                ],
            )
            async with factory() as s:
                up = await media_store.list_media(
                    s, user_id=uid, source="uploaded", page=1, limit=50
                )
                gen = await media_store.list_media(
                    s, user_id=uid, source="generated", page=1, limit=50
                )
            up_sources = {it.metadata["source"] for it in up.items}
            gen_sources = {it.metadata["source"] for it in gen.items}
            # uploaded bucket: exactly the 4 upload-ish sources.
            assert up_sources == {"manual_upload", "upload", "upload_pool", "uploaded"}
            assert up.total == 4
            # generated bucket: everything else.
            assert gen_sources == {"generated", "render_output", "persisted"}
            assert gen.total == 3
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_source_filter_generated_includes_rows_without_source_metadata():
    """Rows with no metadata.source at all are treated as 'generated' (not
    uploads), so the generated bucket isn't silently dropping legacy rows."""

    async def run():
        uid = str(uuid.uuid4())
        engine, factory = await _make_engine_factory()
        try:
            base = datetime(2026, 6, 13, 10, 0, 0, tzinfo=timezone.utc)
            await _seed(
                factory,
                [
                    dict(id=str(uuid.uuid4()), user_id=uid, type="image",
                         file_url="/m/up.png", metadata_={"source": "manual_upload"},
                         created_at=base),
                    dict(id=str(uuid.uuid4()), user_id=uid, type="image",
                         file_url="/m/nosrc.png", metadata_={}, created_at=base),
                ],
            )
            async with factory() as s:
                gen = await media_store.list_media(
                    s, user_id=uid, source="generated", page=1, limit=50
                )
                up = await media_store.list_media(
                    s, user_id=uid, source="uploaded", page=1, limit=50
                )
            assert gen.total == 1  # the source-less row
            assert up.total == 1  # the manual_upload row
        finally:
            await engine.dispose()

    asyncio.run(run())


# ── Bug 6: type facet folds ai_video into "video" ────────────────────────────

def test_type_filter_video_includes_ai_video_rows():
    """Generated videos are stored as type 'ai_video'. Filtering type='video'
    must include them, otherwise the video chip shows nothing on a page of
    generated clips."""

    async def run():
        uid = str(uuid.uuid4())
        engine, factory = await _make_engine_factory()
        try:
            base = datetime(2026, 6, 13, 10, 0, 0, tzinfo=timezone.utc)
            await _seed(
                factory,
                [
                    dict(id=str(uuid.uuid4()), user_id=uid, type="image",
                         file_url="/m/i.png", metadata_={}, created_at=base),
                    dict(id=str(uuid.uuid4()), user_id=uid, type="video",
                         file_url="/m/v.mp4", metadata_={}, created_at=base),
                    dict(id=str(uuid.uuid4()), user_id=uid, type="ai_video",
                         file_url="/m/ai.mp4", metadata_={}, created_at=base),
                ],
            )
            async with factory() as s:
                vids = await media_store.list_media(
                    s, user_id=uid, type_="video", page=1, limit=50
                )
            got_types = sorted(it.type for it in vids.items)
            assert got_types == ["ai_video", "video"]
            assert vids.total == 2
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_media_stats_counts_ai_video_as_video():
    async def run():
        uid = str(uuid.uuid4())
        engine, factory = await _make_engine_factory()
        try:
            base = datetime(2026, 6, 13, 10, 0, 0, tzinfo=timezone.utc)
            await _seed(
                factory,
                [
                    dict(id=str(uuid.uuid4()), user_id=uid, type="ai_video",
                         file_url="/m/ai.mp4", metadata_={"source": "generated"},
                         created_at=base),
                    dict(id=str(uuid.uuid4()), user_id=uid, type="video",
                         file_url="/m/v.mp4", metadata_={"source": "generated"},
                         created_at=base),
                ],
            )
            async with factory() as s:
                stats = await media_store.get_media_stats(s, user_id=uid)
            assert stats.video == 2
            assert stats.total == 2
        finally:
            await engine.dispose()

    asyncio.run(run())


# ── Bug 6: source-aware facet counts ──────────────────────────────────────────

def test_media_stats_reports_uploaded_and_generated_counts():
    """get_media_stats reports per-source bucket counts so the UI can show the
    correct uploaded/generated totals library-wide, not just the current page."""

    async def run():
        uid = str(uuid.uuid4())
        engine, factory = await _make_engine_factory()
        try:
            base = datetime(2026, 6, 13, 10, 0, 0, tzinfo=timezone.utc)
            await _seed(
                factory,
                [
                    dict(id=str(uuid.uuid4()), user_id=uid, type="image",
                         file_url="/m/1.png", metadata_={"source": "manual_upload"},
                         created_at=base),
                    dict(id=str(uuid.uuid4()), user_id=uid, type="video",
                         file_url="/m/2.mp4", metadata_={"source": "generated"},
                         created_at=base),
                    dict(id=str(uuid.uuid4()), user_id=uid, type="video",
                         file_url="/m/3.mp4", metadata_={"source": "render_output"},
                         created_at=base),
                ],
            )
            async with factory() as s:
                stats = await media_store.get_media_stats(s, user_id=uid)
            assert stats.total == 3
            assert stats.image == 1
            assert stats.video == 2
            assert stats.uploaded == 1
            assert stats.generated == 2
        finally:
            await engine.dispose()

    asyncio.run(run())
