"""Tests for the ``exclude_reel_clones`` filter of the media list and the media stats.

A reel the backend saves gives every shot a row of its own: a clone of the user's video
(metadata.cloned_from names it, and the row names no clip). The library must not show those
rows, and a page cut before the clones are dropped would come back nearly empty (the clones
are the newest rows), so the filter runs in the query: before limit and offset, and the
total and the stats count the same set.

A clone that a clip holds (the clip editor's split carries a clip_id) is an item of the user's
and stays; so does a row whose cloned_from is empty.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(element, compiler, **kw):  # pragma: no cover - trivial
    return "JSON"


@compiles(UUID, "sqlite")
def _uuid_sqlite(element, compiler, **kw):  # pragma: no cover - trivial
    return "VARCHAR(36)"


from app.db import get_session  # noqa: E402
from app.fastapi_app import create_fastapi_app  # noqa: E402
from app.models import MediaItem  # noqa: E402  (after compiler registration)
from app.schemas import PagedResponse  # noqa: E402
from app.stores import media as media_store  # noqa: E402

INTERNAL_SECRET = "test-secret-xyz"
AUTH_HEADERS = {"X-Internal-Secret": INTERNAL_SECRET, "X-User-ID": "00000000-0000-0000-0000-0000000000a1"}

BASE = datetime(2026, 6, 13, 10, 0, 0, tzinfo=timezone.utc)


async def _make_engine_factory():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: MediaItem.__table__.create(c))
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _seed(factory, rows: list[dict]):
    async with factory() as s:
        for r in rows:
            s.add(MediaItem(**r))
        await s.commit()


def _row(uid, n, *, minutes, metadata=None, clip_id=None, type_="ai_video"):
    return dict(
        id=str(uuid.uuid4()),
        user_id=uid,
        clip_id=clip_id,
        type=type_,
        file_url=f"/m/{n}.mp4",
        metadata_=metadata if metadata is not None else {},
        created_at=BASE + timedelta(minutes=minutes),
    )


def _clone(uid, n, *, minutes, clip_id=None, cloned_from="source-video"):
    return _row(uid, n, minutes=minutes, metadata={"cloned_from": cloned_from, "attached_from": "manifest"}, clip_id=clip_id)


def _run(coro):
    return asyncio.run(coro)


def test_a_page_with_the_flag_is_the_users_videos_not_the_clones_in_front_of_them():
    """25 clones newer than 5 videos: page 1 (limit 20) has the 5 videos and total 5."""

    async def run():
        uid = str(uuid.uuid4())
        engine, factory = await _make_engine_factory()
        try:
            videos = [_row(uid, i, minutes=i) for i in range(5)]
            clones = [_clone(uid, 100 + i, minutes=100 + i) for i in range(25)]
            await _seed(factory, videos + clones)
            async with factory() as s:
                page = await media_store.list_media(s, user_id=uid, page=1, limit=20, exclude_reel_clones=True)
                typed = await media_store.list_media(s, user_id=uid, type_="video", page=1, limit=20, exclude_reel_clones=True)
            assert page.total == 5
            assert {item.id for item in page.items} == {v["id"] for v in videos}
            assert typed.total == 5 and len(typed.items) == 5
        finally:
            await engine.dispose()

    _run(run())


def test_without_the_flag_the_list_is_what_it_was():
    async def run():
        uid = str(uuid.uuid4())
        engine, factory = await _make_engine_factory()
        try:
            await _seed(
                factory,
                [_row(uid, i, minutes=i) for i in range(5)] + [_clone(uid, 100 + i, minutes=100 + i) for i in range(25)],
            )
            async with factory() as s:
                default = await media_store.list_media(s, user_id=uid, page=1, limit=20)
                off = await media_store.list_media(s, user_id=uid, page=1, limit=20, exclude_reel_clones=False)
            for page in (default, off):
                assert page.total == 30
                assert len(page.items) == 20
                assert all(item.metadata.get("cloned_from") for item in page.items)  # the newest rows are the clones
        finally:
            await engine.dispose()

    _run(run())


def test_the_pages_of_a_filtered_list_are_disjoint_and_whole():
    async def run():
        uid = str(uuid.uuid4())
        engine, factory = await _make_engine_factory()
        try:
            videos = [_row(uid, i, minutes=i * 10) for i in range(7)]
            clones = [_clone(uid, 100 + i, minutes=i * 10 + 5) for i in range(7)]  # a clone between every two videos
            await _seed(factory, videos + clones)
            seen = []
            async with factory() as s:
                for page in (1, 2, 3, 4):
                    result = await media_store.list_media(s, user_id=uid, page=page, limit=3, exclude_reel_clones=True)
                    assert result.total == 7
                    seen += [item.id for item in result.items]
            assert sorted(seen) == sorted(v["id"] for v in videos)
        finally:
            await engine.dispose()

    _run(run())


def test_a_clone_a_clip_holds_stays():
    """The clip editor's split carries its clip's id: an item of the user's."""

    async def run():
        uid = str(uuid.uuid4())
        engine, factory = await _make_engine_factory()
        try:
            video = _row(uid, 1, minutes=1)
            held = _clone(uid, 2, minutes=2, clip_id=str(uuid.uuid4()))
            loose = _clone(uid, 3, minutes=3)
            await _seed(factory, [video, held, loose])
            async with factory() as s:
                result = await media_store.list_media(s, user_id=uid, exclude_reel_clones=True)
            assert result.total == 2
            assert {item.id for item in result.items} == {video["id"], held["id"]}
        finally:
            await engine.dispose()

    _run(run())


def test_an_empty_cloned_from_is_not_a_clone():
    async def run():
        uid = str(uuid.uuid4())
        engine, factory = await _make_engine_factory()
        try:
            rows = {
                "empty": _row(uid, 1, minutes=1, metadata={"cloned_from": ""}),
                "blank": _row(uid, 2, minutes=2, metadata={"cloned_from": "  "}),
                "null": _row(uid, 3, minutes=3, metadata={"cloned_from": None}),
                "absent": _row(uid, 4, minutes=4, metadata={"source": "manual_upload"}),
                "no metadata": _row(uid, 5, minutes=5, metadata={}),
                "clone": _clone(uid, 6, minutes=6),
            }
            await _seed(factory, list(rows.values()))
            async with factory() as s:
                result = await media_store.list_media(s, user_id=uid, exclude_reel_clones=True)
            assert {item.id for item in result.items} == {r["id"] for name, r in rows.items() if name != "clone"}
            assert result.total == 5
        finally:
            await engine.dispose()

    _run(run())


def test_the_filter_is_the_users_own_and_combines_with_the_others():
    async def run():
        uid, other = str(uuid.uuid4()), str(uuid.uuid4())
        engine, factory = await _make_engine_factory()
        try:
            mine = _row(uid, 1, minutes=1, metadata={"source": "manual_upload"})
            image = _row(uid, 2, minutes=2, type_="image")
            theirs = _row(other, 3, minutes=3)
            await _seed(factory, [mine, image, theirs, _clone(uid, 4, minutes=4), _clone(other, 5, minutes=5)])
            async with factory() as s:
                everything = await media_store.list_media(s, user_id=uid, exclude_reel_clones=True)
                uploads = await media_store.list_media(s, user_id=uid, source="uploaded", exclude_reel_clones=True)
                images = await media_store.list_media(s, user_id=uid, type_="image", exclude_reel_clones=True)
            assert {i.id for i in everything.items} == {mine["id"], image["id"]}
            assert [i.id for i in uploads.items] == [mine["id"]] and uploads.total == 1
            assert [i.id for i in images.items] == [image["id"]] and images.total == 1
        finally:
            await engine.dispose()

    _run(run())


def test_the_stats_count_the_same_set():
    async def run():
        uid = str(uuid.uuid4())
        engine, factory = await _make_engine_factory()
        try:
            videos = [_row(uid, i, minutes=i) for i in range(5)]
            await _seed(
                factory,
                videos
                + [_row(uid, 9, minutes=9, type_="image", metadata={"source": "manual_upload"})]
                + [_clone(uid, 100 + i, minutes=100 + i) for i in range(25)]
                + [_clone(uid, 200, minutes=200, clip_id=str(uuid.uuid4()))],
            )
            async with factory() as s:
                library = await media_store.get_media_stats(s, user_id=uid, exclude_reel_clones=True)
                everything = await media_store.get_media_stats(s, user_id=uid)
            assert (library.total, library.video, library.image, library.uploaded, library.generated) == (7, 6, 1, 1, 6)
            assert (everything.total, everything.video) == (32, 31)
        finally:
            await engine.dispose()

    _run(run())


# ── the routes ───────────────────────────────────────────────────────────────

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_SECRET", INTERNAL_SECRET)

    async def mock_session():
        yield MagicMock()

    app = create_fastapi_app()
    app.dependency_overrides[get_session] = mock_session
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_the_list_route_forwards_the_flag(client):
    paged = PagedResponse(items=[], total=0, page=1, limit=50)
    with patch("app.stores.media.list_media", new=AsyncMock(return_value=paged)) as listed:
        assert client.get("/v1/media", params={"exclude_reel_clones": "true"}, headers=AUTH_HEADERS).status_code == 200
        assert listed.await_args.kwargs["exclude_reel_clones"] is True
        assert client.get("/v1/media", headers=AUTH_HEADERS).status_code == 200
        assert listed.await_args.kwargs["exclude_reel_clones"] is False


def test_the_stats_route_forwards_the_flag(client):
    from app.schemas import MediaStatsOut

    stats = MediaStatsOut(total=0, image=0, video=0, audio=0)
    with patch("app.stores.media.get_media_stats", new=AsyncMock(return_value=stats)) as counted:
        assert client.get("/v1/media/stats", params={"exclude_reel_clones": "true"}, headers=AUTH_HEADERS).status_code == 200
        assert counted.await_args.kwargs["exclude_reel_clones"] is True
        assert client.get("/v1/media/stats", headers=AUTH_HEADERS).status_code == 200
        assert counted.await_args.kwargs["exclude_reel_clones"] is False
