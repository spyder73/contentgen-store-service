"""Tests for cross-user upsert rejection on pipeline + prompt templates.

``upsert_pipeline`` and ``upsert_prompt`` used to silently no-op -- returning
the existing row unchanged -- when a non-admin caller tried to overwrite a
template owned by someone else. The caller got back a 200 and believed the
write had succeeded, when nothing was actually persisted. Both stores now
raise a domain error (``PipelineTemplateError`` / ``PromptTemplateError``,
carrying an HTTP status) instead, which the routes in app/fastapi_app.py
translate into a 403.

This module verifies:
  - a non-owner, non-admin caller gets rejected (403) and the row is untouched
  - the legitimate owner can still update their own row
  - an admin can still upsert (and reassign) any row
  - the ownership-adoption path -- an unowned row picked up by the first
    caller to touch it -- still works
"""
from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(element, compiler, **kw):  # pragma: no cover - trivial
    return "JSON"


@compiles(UUID, "sqlite")
def _uuid_sqlite(element, compiler, **kw):  # pragma: no cover - trivial
    return "VARCHAR(36)"


from app.models import PipelineTemplate, PipelineTemplateAssignment, PromptTemplate  # noqa: E402
from app.schemas import PipelineTemplateIn, PromptTemplateIn  # noqa: E402
from app.stores import pipelines as pipelines_store  # noqa: E402
from app.stores import prompts as prompts_store  # noqa: E402
from app.stores.pipelines import PipelineTemplateError  # noqa: E402
from app.stores.prompts import PromptTemplateError  # noqa: E402


async def _pipeline_engine_factory():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: PipelineTemplate.__table__.create(c))
        await conn.run_sync(lambda c: PipelineTemplateAssignment.__table__.create(c))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    return engine, factory


async def _prompt_engine_factory():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: PromptTemplate.__table__.create(c))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    return engine, factory


# ── pipeline templates ───────────────────────────────────────────────────────


def test_pipeline_upsert_rejects_cross_user_overwrite():
    """A non-owner, non-admin caller gets a 403-mapped error, not a silent 200."""

    async def run():
        owner = str(uuid.uuid4())
        other = str(uuid.uuid4())
        engine, factory = await _pipeline_engine_factory()
        try:
            async with factory() as s:
                s.add(
                    PipelineTemplate(
                        id="pt-1", name="Original", data={"a": 1}, version=1,
                        user_id=owner, visibility="private",
                    )
                )
                await s.commit()

            body = PipelineTemplateIn(id="pt-1", name="Hijacked", data={"a": 2}, version=2)
            async with factory() as s:
                with pytest.raises(PipelineTemplateError) as exc:
                    await pipelines_store.upsert_pipeline(s, body, user_id=other, admin=False)
            assert exc.value.status_code == 403

            async with factory() as s:
                row = await s.get(PipelineTemplate, "pt-1")
                assert row.name == "Original"
                assert row.data == {"a": 1}
                assert row.user_id == owner
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_pipeline_upsert_rejects_unowned_non_global_overwrite():
    """An unowned row that isn't global (edge state) is also rejected, not adopted."""

    async def run():
        caller = str(uuid.uuid4())
        engine, factory = await _pipeline_engine_factory()
        try:
            async with factory() as s:
                s.add(
                    PipelineTemplate(
                        id="pt-2", name="Orphan", data={}, version=1,
                        user_id=None, visibility="private",
                    )
                )
                await s.commit()

            body = PipelineTemplateIn(id="pt-2", name="Grabbed", data={"x": 1}, version=2)
            async with factory() as s:
                with pytest.raises(PipelineTemplateError) as exc:
                    await pipelines_store.upsert_pipeline(s, body, user_id=caller, admin=False)
            assert exc.value.status_code == 403

            async with factory() as s:
                row = await s.get(PipelineTemplate, "pt-2")
                assert row.name == "Orphan"
                assert row.user_id is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_pipeline_upsert_owner_can_still_update_own_template():
    async def run():
        owner = str(uuid.uuid4())
        engine, factory = await _pipeline_engine_factory()
        try:
            async with factory() as s:
                s.add(
                    PipelineTemplate(
                        id="pt-3", name="Original", data={"a": 1}, version=1,
                        user_id=owner, visibility="private",
                    )
                )
                await s.commit()

            body = PipelineTemplateIn(id="pt-3", name="Updated", data={"a": 2}, version=2)
            async with factory() as s:
                out = await pipelines_store.upsert_pipeline(s, body, user_id=owner, admin=False)
            assert out.name == "Updated"
            assert out.data == {"a": 2}
            assert out.user_id == owner
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_pipeline_upsert_admin_can_overwrite_and_reassign():
    """Admin path bypasses the ownership guard entirely (no exception).

    ``set_pipeline_assignments`` is patched out here: it has a pre-existing,
    unrelated bug where its internal ``commit=False`` call path reads
    ``row.updated_at`` before the row is refreshed, which only surfaces under
    the sqlite test driver (asyncpg/Postgres populates server-side defaults
    via RETURNING on flush; aiosqlite does not) -- not something introduced or
    fixed by this change, and out of scope for the ownership-guard hardening
    under test here.
    """

    async def run():
        owner = str(uuid.uuid4())
        new_owner = str(uuid.uuid4())
        admin_id = str(uuid.uuid4())
        engine, factory = await _pipeline_engine_factory()
        try:
            async with factory() as s:
                s.add(
                    PipelineTemplate(
                        id="pt-4", name="Original", data={"a": 1}, version=1,
                        user_id=owner, visibility="private",
                    )
                )
                await s.commit()

            body = PipelineTemplateIn(
                id="pt-4", name="Admin Edit", data={"a": 9}, version=2,
                user_id=new_owner, visibility="private",
            )
            async with factory() as s:
                with patch(
                    "app.stores.pipelines.set_pipeline_assignments",
                    new=AsyncMock(return_value=None),
                ):
                    out = await pipelines_store.upsert_pipeline(s, body, user_id=admin_id, admin=True)
            assert out.name == "Admin Edit"
            assert out.user_id == new_owner
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_pipeline_upsert_adopts_unowned_global_template():
    """An unowned, globally-visible row is adopted by the first non-admin caller."""

    async def run():
        adopter = str(uuid.uuid4())
        engine, factory = await _pipeline_engine_factory()
        try:
            async with factory() as s:
                s.add(
                    PipelineTemplate(
                        id="pt-5", name="Global Orphan", data={}, version=1,
                        user_id=None, visibility="global",
                    )
                )
                await s.commit()

            body = PipelineTemplateIn(id="pt-5", name="Adopted", data={"x": 1}, version=2)
            async with factory() as s:
                out = await pipelines_store.upsert_pipeline(s, body, user_id=adopter, admin=False)
            assert out.name == "Adopted"
            assert out.user_id == adopter
        finally:
            await engine.dispose()

    asyncio.run(run())


# ── prompt templates ─────────────────────────────────────────────────────────


def test_prompt_upsert_rejects_cross_user_overwrite():
    async def run():
        owner = str(uuid.uuid4())
        other = str(uuid.uuid4())
        engine, factory = await _prompt_engine_factory()
        try:
            async with factory() as s:
                s.add(
                    PromptTemplate(
                        id="pr-1", name="Original", content="c1", metadata_={},
                        user_id=owner, visibility="private",
                    )
                )
                await s.commit()

            body = PromptTemplateIn(id="pr-1", name="Hijacked", content="c2")
            async with factory() as s:
                with pytest.raises(PromptTemplateError) as exc:
                    await prompts_store.upsert_prompt(s, body, user_id=other, admin=False)
            assert exc.value.status_code == 403

            async with factory() as s:
                row = await s.get(PromptTemplate, "pr-1")
                assert row.name == "Original"
                assert row.content == "c1"
                assert row.user_id == owner
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_prompt_upsert_owner_can_still_update_own_template():
    async def run():
        owner = str(uuid.uuid4())
        engine, factory = await _prompt_engine_factory()
        try:
            async with factory() as s:
                s.add(
                    PromptTemplate(
                        id="pr-2", name="Original", content="c1", metadata_={},
                        user_id=owner, visibility="private",
                    )
                )
                await s.commit()

            body = PromptTemplateIn(id="pr-2", name="Updated", content="c2")
            async with factory() as s:
                out = await prompts_store.upsert_prompt(s, body, user_id=owner, admin=False)
            assert out.name == "Updated"
            assert out.content == "c2"
            assert out.user_id == owner
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_prompt_upsert_admin_can_overwrite_and_reassign():
    async def run():
        owner = str(uuid.uuid4())
        new_owner = str(uuid.uuid4())
        admin_id = str(uuid.uuid4())
        engine, factory = await _prompt_engine_factory()
        try:
            async with factory() as s:
                s.add(
                    PromptTemplate(
                        id="pr-3", name="Original", content="c1", metadata_={},
                        user_id=owner, visibility="private",
                    )
                )
                await s.commit()

            body = PromptTemplateIn(
                id="pr-3", name="Admin Edit", content="c9",
                user_id=new_owner, visibility="global",
            )
            async with factory() as s:
                out = await prompts_store.upsert_prompt(s, body, user_id=admin_id, admin=True)
            assert out.name == "Admin Edit"
            assert out.user_id == new_owner
            assert out.visibility == "global"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_prompt_upsert_sets_owner_on_first_create():
    """Ownership assignment on brand-new rows (create path) is unaffected."""

    async def run():
        creator = str(uuid.uuid4())
        engine, factory = await _prompt_engine_factory()
        try:
            body = PromptTemplateIn(id="pr-4", name="New", content="c1")
            async with factory() as s:
                out = await prompts_store.upsert_prompt(s, body, user_id=creator, admin=False)
            assert out.user_id == creator
            assert out.visibility == "private"
        finally:
            await engine.dispose()

    asyncio.run(run())
