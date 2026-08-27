"""Tests for the media-retention sweep (`POST /v1/maintenance/media-retention`).

A rendered clip is the only media class whose bytes are reproducible — the
scene images, prompts and render template that produced it are retained — so
after the window its ``file_data``/``thumbnail_data``/``micro_thumbnail`` may be
dropped and the row stamped ``bytes_evicted``. Everything else is a library
original and must survive; the exclusion clauses are the part worth testing,
because a regression there silently destroys LoRA identity references.

DB-integration against in-memory sqlite, mirroring `test_media_lineage.py`.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

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


from app.models import Character, DatasetTemplate, MediaItem  # noqa: E402
from app.stores import media as media_store  # noqa: E402


async def _make_factory():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        for model in (MediaItem, Character, DatasetTemplate):
            await conn.run_sync(lambda c, m=model: m.__table__.create(c))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    return engine, factory


def _days_ago(n: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=n)


async def _add_render(
    factory,
    *,
    mid: str,
    age_days: int = 30,
    source: str = "render_output",
    is_favourite: bool = False,
    parent: str | None = None,
    metadata: dict | None = None,
    with_proxy: bool = True,
):
    """Insert a media row carrying every byte column the sweep can touch."""
    meta = {"source": source}
    if metadata:
        meta.update(metadata)
    async with factory() as s:
        s.add(
            MediaItem(
                id=mid,
                user_id=str(uuid.uuid4()),
                type="ai_video",
                prompt="p",
                file_url=f"/media/{mid}.mp4",
                metadata_=meta,
                is_favourite=is_favourite,
                parent_media_id=parent,
                file_data=b"f" * 100,
                file_mime_type="video/mp4",
                thumbnail_data=b"t" * 20,
                thumbnail_content_type="image/webp",
                micro_thumbnail="data:image/webp;base64,AAAA",
                proxy_bytes=b"p" * 50 if with_proxy else None,
                proxy_mime="video/mp4" if with_proxy else None,
                created_at=_days_ago(age_days),
            )
        )
        await s.commit()


async def _get(factory, mid: str) -> MediaItem:
    async with factory() as s:
        row = await s.get(MediaItem, mid)
        assert row is not None
        return row


async def _sweep(factory, *, older_than_days: int = 14, dry_run: bool = False):
    async with factory() as s:
        return await media_store.sweep_media_retention(
            s, older_than_days=older_than_days, dry_run=dry_run
        )


def _assert_intact(row: MediaItem) -> None:
    assert row.file_data is not None
    assert row.thumbnail_data is not None
    assert row.micro_thumbnail is not None
    assert "bytes_evicted" not in (row.metadata_ or {})


def _assert_stripped(row: MediaItem) -> None:
    assert row.file_data is None
    assert row.thumbnail_data is None
    assert row.micro_thumbnail is None
    assert (row.metadata_ or {}).get("bytes_evicted") is True


# ── the eligible case ────────────────────────────────────────────────────────

def test_old_render_output_is_stripped_and_stamped():
    async def run():
        engine, factory = await _make_factory()
        try:
            mid = str(uuid.uuid4())
            await _add_render(factory, mid=mid, age_days=30)

            result = await _sweep(factory)

            assert result.stripped_rows == 1
            assert result.stripped_bytes > 0
            _assert_stripped(await _get(factory, mid))
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_render_output_inside_the_window_is_untouched():
    async def run():
        engine, factory = await _make_factory()
        try:
            mid = str(uuid.uuid4())
            await _add_render(factory, mid=mid, age_days=3)

            result = await _sweep(factory, older_than_days=14)

            assert result.stripped_rows == 0
            _assert_intact(await _get(factory, mid))
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_non_render_output_is_never_stripped():
    """A generated image / upload has no re-render path — its bytes are the only copy."""

    async def run():
        engine, factory = await _make_factory()
        try:
            mid = str(uuid.uuid4())
            await _add_render(factory, mid=mid, age_days=90, source="generated")

            result = await _sweep(factory)

            assert result.stripped_rows == 0
            _assert_intact(await _get(factory, mid))
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_second_run_is_a_no_op():
    """Idempotence: the nightly caller may run twice; already-stripped rows are
    neither rewritten nor re-counted."""

    async def run():
        engine, factory = await _make_factory()
        try:
            mid = str(uuid.uuid4())
            await _add_render(factory, mid=mid, age_days=30)

            first = await _sweep(factory)
            second = await _sweep(factory)

            assert first.stripped_rows == 1
            assert second.stripped_rows == 0
            assert second.stripped_bytes == 0
            assert second.proxies_cleared == 0
        finally:
            await engine.dispose()

    asyncio.run(run())


# ── exclusions ───────────────────────────────────────────────────────────────

def test_favourite_is_excluded():
    async def run():
        engine, factory = await _make_factory()
        try:
            mid = str(uuid.uuid4())
            await _add_render(factory, mid=mid, age_days=30, is_favourite=True)

            result = await _sweep(factory)

            assert result.stripped_rows == 0
            _assert_intact(await _get(factory, mid))
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_character_reference_image_is_excluded():
    """characters.reference_image_media_id is ON DELETE SET NULL — that protects
    the row from a DELETE but not its bytes from this UPDATE."""

    async def run():
        engine, factory = await _make_factory()
        try:
            mid = str(uuid.uuid4())
            await _add_render(factory, mid=mid, age_days=30)
            async with factory() as s:
                s.add(
                    Character(
                        id=str(uuid.uuid4()),
                        series_id=str(uuid.uuid4()),
                        name="Ana",
                        reference_image_media_id=mid,
                        metadata_={},
                    )
                )
                await s.commit()

            result = await _sweep(factory)

            assert result.stripped_rows == 0
            _assert_intact(await _get(factory, mid))
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_dataset_template_seed_reference_is_excluded():
    """seed_reference_media_id is a plain Text column with NO foreign key — it is
    the identity anchor for every collage stage of a LoRA dataset."""

    async def run():
        engine, factory = await _make_factory()
        try:
            mid = str(uuid.uuid4())
            await _add_render(factory, mid=mid, age_days=30)
            async with factory() as s:
                s.add(
                    DatasetTemplate(
                        id=str(uuid.uuid4()),
                        name="tpl",
                        collage_prompt="c",
                        seed_reference_media_id=mid,
                    )
                )
                await s.commit()

            result = await _sweep(factory)

            assert result.stripped_rows == 0
            _assert_intact(await _get(factory, mid))
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_parent_of_another_row_is_excluded():
    async def run():
        engine, factory = await _make_factory()
        try:
            parent = str(uuid.uuid4())
            child = str(uuid.uuid4())
            await _add_render(factory, mid=parent, age_days=30)
            await _add_render(factory, mid=child, age_days=30, parent=parent)

            result = await _sweep(factory)

            # Only the leaf is eligible; the parent keeps its bytes.
            assert result.stripped_rows == 1
            _assert_intact(await _get(factory, parent))
            _assert_stripped(await _get(factory, child))
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_unrelated_rows_do_not_protect_each_other():
    """A NULL-heavy reference table must not make the sweep a no-op (the NOT IN
    NULL trap the exclusions are written as NOT EXISTS to avoid)."""

    async def run():
        engine, factory = await _make_factory()
        try:
            mid = str(uuid.uuid4())
            await _add_render(factory, mid=mid, age_days=30)
            async with factory() as s:
                s.add(
                    Character(
                        id=str(uuid.uuid4()),
                        series_id=str(uuid.uuid4()),
                        name="No reference",
                        reference_image_media_id=None,
                        metadata_={},
                    )
                )
                s.add(
                    DatasetTemplate(
                        id=str(uuid.uuid4()),
                        name="tpl",
                        collage_prompt="c",
                        seed_reference_media_id=None,
                    )
                )
                await s.commit()

            result = await _sweep(factory)

            assert result.stripped_rows == 1
            _assert_stripped(await _get(factory, mid))
        finally:
            await engine.dispose()

    asyncio.run(run())


# ── metadata merge ───────────────────────────────────────────────────────────

def test_metadata_merge_preserves_existing_keys():
    async def run():
        engine, factory = await _make_factory()
        try:
            mid = str(uuid.uuid4())
            await _add_render(
                factory,
                mid=mid,
                age_days=30,
                metadata={"provenance": {"ai": True}, "pipeline_run_id": "run-1"},
            )

            await _sweep(factory)

            row = await _get(factory, mid)
            assert row.metadata_["bytes_evicted"] is True
            assert row.metadata_["source"] == "render_output"
            assert row.metadata_["provenance"] == {"ai": True}
            assert row.metadata_["pipeline_run_id"] == "run-1"
        finally:
            await engine.dispose()

    asyncio.run(run())


# ── proxy cache clear ────────────────────────────────────────────────────────

def test_proxy_is_cleared_on_non_render_rows_too():
    """The 480p proxy is a pure cache the Go backend regenerates on demand, so it
    goes for ANY row past the cutoff — including ones excluded from stripping."""

    async def run():
        engine, factory = await _make_factory()
        try:
            generated = str(uuid.uuid4())
            favourite = str(uuid.uuid4())
            await _add_render(factory, mid=generated, age_days=30, source="generated")
            await _add_render(factory, mid=favourite, age_days=30, is_favourite=True)

            result = await _sweep(factory)

            assert result.stripped_rows == 0
            assert result.proxies_cleared == 2
            assert result.proxy_bytes_freed == 100
            for mid in (generated, favourite):
                row = await _get(factory, mid)
                assert row.proxy_bytes is None
                _assert_intact(row)  # only the cache went
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_proxy_inside_the_window_is_kept():
    async def run():
        engine, factory = await _make_factory()
        try:
            mid = str(uuid.uuid4())
            await _add_render(factory, mid=mid, age_days=2, source="generated")

            result = await _sweep(factory)

            assert result.proxies_cleared == 0
            assert (await _get(factory, mid)).proxy_bytes is not None
        finally:
            await engine.dispose()

    asyncio.run(run())


# ── dry run ──────────────────────────────────────────────────────────────────

def test_dry_run_reports_the_same_counts_and_writes_nothing():
    async def run():
        engine, factory = await _make_factory()
        try:
            stripped = str(uuid.uuid4())
            proxy_only = str(uuid.uuid4())
            await _add_render(factory, mid=stripped, age_days=30)
            await _add_render(factory, mid=proxy_only, age_days=30, source="generated")

            preview = await _sweep(factory, dry_run=True)

            assert preview.dry_run is True
            row = await _get(factory, stripped)
            _assert_intact(row)
            assert row.proxy_bytes is not None

            applied = await _sweep(factory, dry_run=False)

            assert applied.stripped_rows == preview.stripped_rows == 1
            assert applied.stripped_bytes == preview.stripped_bytes
            assert applied.proxies_cleared == preview.proxies_cleared == 2
            assert applied.proxy_bytes_freed == preview.proxy_bytes_freed
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_older_than_days_must_be_at_least_one():
    async def run():
        engine, factory = await _make_factory()
        try:
            with pytest.raises(ValueError):
                await _sweep(factory, older_than_days=0)
        finally:
            await engine.dispose()

    asyncio.run(run())


# ── route ────────────────────────────────────────────────────────────────────

INTERNAL_SECRET = "test-secret-retention"


@pytest.fixture(autouse=True)
def _internal_secret_env(monkeypatch):
    # Pinned per-test: other modules overwrite this env var and run order would
    # otherwise 401 us.
    monkeypatch.setenv("INTERNAL_API_SECRET", INTERNAL_SECRET)


from app.db import get_session  # noqa: E402
from app.fastapi_app import create_fastapi_app  # noqa: E402
from app.schemas import MediaRetentionOut  # noqa: E402

AUTH_HEADERS = {"X-Internal-Secret": INTERNAL_SECRET}
ENDPOINT = "/v1/maintenance/media-retention"


async def _mock_session():
    yield None


@pytest.fixture()
def client():
    app = create_fastapi_app()
    app.dependency_overrides[get_session] = _mock_session
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


def _result(**over) -> MediaRetentionOut:
    payload = dict(
        dry_run=True,
        older_than_days=14,
        cutoff=datetime.now(timezone.utc),
        stripped_rows=3,
        stripped_bytes=999,
        proxies_cleared=5,
        proxy_bytes_freed=42,
    )
    payload.update(over)
    return MediaRetentionOut(**payload)


class TestMediaRetentionRoute:
    def test_requires_internal_secret(self, client):
        resp = client.post(ENDPOINT, json={"older_than_days": 14})
        assert resp.status_code == 401

    def test_body_params_are_forwarded(self, client):
        sweep = AsyncMock(return_value=_result(dry_run=False))
        with patch("app.stores.media.sweep_media_retention", new=sweep):
            resp = client.post(
                ENDPOINT,
                json={"older_than_days": 14, "dry_run": False},
                headers=AUTH_HEADERS,
            )
        assert resp.status_code == 200
        assert resp.json()["stripped_rows"] == 3
        assert sweep.await_args.kwargs["older_than_days"] == 14
        assert sweep.await_args.kwargs["dry_run"] is False

    def test_query_params_are_accepted(self, client):
        sweep = AsyncMock(return_value=_result(dry_run=False, older_than_days=30))
        with patch("app.stores.media.sweep_media_retention", new=sweep):
            resp = client.post(
                f"{ENDPOINT}?older_than_days=30&dry_run=false", headers=AUTH_HEADERS
            )
        assert resp.status_code == 200
        assert sweep.await_args.kwargs["older_than_days"] == 30
        assert sweep.await_args.kwargs["dry_run"] is False

    def test_dry_run_defaults_to_true(self, client):
        sweep = AsyncMock(return_value=_result())
        with patch("app.stores.media.sweep_media_retention", new=sweep):
            resp = client.post(ENDPOINT, json={"older_than_days": 14}, headers=AUTH_HEADERS)
        assert resp.status_code == 200
        assert sweep.await_args.kwargs["dry_run"] is True

    def test_older_than_days_is_required(self, client):
        resp = client.post(ENDPOINT, headers=AUTH_HEADERS)
        assert resp.status_code == 422

    def test_zero_window_is_rejected(self, client):
        resp = client.post(ENDPOINT, json={"older_than_days": 0}, headers=AUTH_HEADERS)
        assert resp.status_code == 422
