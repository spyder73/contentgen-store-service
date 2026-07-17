"""Tests for prompt-template list scoping.

The Go backend rehydrates its in-memory PromptStore on every startup via an
*unscoped* GET /v1/prompts (no X-User-ID header -> user_id=None). That in-memory
store is the single source `validatePromptTemplateRefs` consults, so if the
unscoped list drops user-owned (private) templates, every pipeline that
references one fails save-validation with "unknown prompt_template_id" after a
restart -- even though the template still exists in the DB.

`list_pipelines(user_id=None)` already returns ALL rows; `list_prompts` must
match, or prompt refs and pipeline refs rehydrate asymmetrically.
"""
from __future__ import annotations

import asyncio
import uuid

from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(element, compiler, **kw):  # pragma: no cover - trivial
    return "JSON"


@compiles(UUID, "sqlite")
def _uuid_sqlite(element, compiler, **kw):  # pragma: no cover - trivial
    return "VARCHAR(36)"


from app.models import PromptTemplate  # noqa: E402  (after compiler registration)
from app.stores import prompts as prompts_store  # noqa: E402


async def _make_engine_factory():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: PromptTemplate.__table__.create(c))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    return engine, factory


async def _seed(factory, rows: list[dict]):
    async with factory() as s:
        for r in rows:
            s.add(PromptTemplate(**r))
        await s.commit()


def _rows(user_a: str) -> list[dict]:
    return [
        dict(id="global-one", name="Global One", content="c", metadata_={}, user_id=None, visibility="global"),
        dict(id="user-a-private", name="A Private", content="c", metadata_={}, user_id=user_a, visibility="private"),
        dict(id="user-b-private", name="B Private", content="c", metadata_={}, user_id=str(uuid.uuid4()), visibility="private"),
    ]


def test_unscoped_list_returns_all_including_private():
    """The backend boot-load (no X-User-ID) must see every template, not just globals."""

    async def run():
        user_a = str(uuid.uuid4())
        engine, factory = await _make_engine_factory()
        try:
            await _seed(factory, _rows(user_a))
            async with factory() as s:
                out = await prompts_store.list_prompts(s, user_id=None)
            ids = {p.id for p in out}
            assert ids == {"global-one", "user-a-private", "user-b-private"}
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_user_scoped_list_returns_own_plus_global_only():
    """A user-scoped list still only exposes the caller's own + global templates."""

    async def run():
        user_a = str(uuid.uuid4())
        engine, factory = await _make_engine_factory()
        try:
            await _seed(factory, _rows(user_a))
            async with factory() as s:
                out = await prompts_store.list_prompts(s, user_id=user_a)
            ids = {p.id for p in out}
            assert ids == {"global-one", "user-a-private"}
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_admin_list_returns_all():
    """An admin list is unfiltered regardless of user_id."""

    async def run():
        user_a = str(uuid.uuid4())
        engine, factory = await _make_engine_factory()
        try:
            await _seed(factory, _rows(user_a))
            async with factory() as s:
                out = await prompts_store.list_prompts(s, user_id=user_a, admin=True)
            ids = {p.id for p in out}
            assert ids == {"global-one", "user-a-private", "user-b-private"}
        finally:
            await engine.dispose()

    asyncio.run(run())
