"""series.format_brief: the per-series format brief the crew agents read.

Migration 0041 adds ``series.format_brief TEXT NOT NULL DEFAULT ''``. The brief
is the only place a show's genre and style live, so it must survive the PUT the
not-yet-redeployed frontend still sends (the v1 shape, with no such key):

  * an absent or ``null`` ``format_brief`` keeps the stored value;
  * any string, even ``""``, is written.

DB-level assertions run against in-memory sqlite over the real ORM table
(mirroring test_series_v2.py); the migration's own DDL is asserted by rendering
it offline in the postgres dialect.
"""
from __future__ import annotations

import asyncio
import importlib.util
import uuid
from pathlib import Path

from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(element, compiler, **kw):  # pragma: no cover - trivial
    return "JSON"


@compiles(UUID, "sqlite")
def _uuid_sqlite(element, compiler, **kw):  # pragma: no cover - trivial
    return "VARCHAR(36)"


from app.models import Series, User  # noqa: E402
from app.schemas import SeriesIn  # noqa: E402
from app.stores import series as series_store  # noqa: E402

USER_A = str(uuid.uuid4())

MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "0041_series_format_brief.py"
)


async def _make_factory():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        for table in (User.__table__, Series.__table__):
            await conn.run_sync(lambda c, t=table: t.create(c))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        s.add(User(id=USER_A, username="a", password_hash="x"))
        await s.commit()
    return engine, factory


async def _put(factory, body: SeriesIn):
    async with factory() as s:
        return await series_store.upsert_series(s, body, user_id=USER_A)


async def _get(factory, sid: str):
    async with factory() as s:
        row = await series_store.get_series(s, sid, user_id=USER_A)
    assert row is not None
    return row


# ── series ───────────────────────────────────────────────────────────────────


def test_format_brief_roundtrips():
    async def run():
        engine, factory = await _make_factory()
        try:
            sid = str(uuid.uuid4())
            written = await _put(
                factory,
                SeriesIn(id=sid, name="Coconut Chronicles", format_brief="Open on a face."),
            )
            assert written.format_brief == "Open on a face."
            assert (await _get(factory, sid)).format_brief == "Open on a face."
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_format_brief_defaults_empty():
    """A show created by a client that knows nothing of the brief reads back ''."""

    async def run():
        engine, factory = await _make_factory()
        try:
            sid = str(uuid.uuid4())
            written = await _put(factory, SeriesIn(id=sid, name="Coconut Chronicles"))
            assert written.format_brief == ""
            assert (await _get(factory, sid)).format_brief == ""
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_old_shape_put_keeps_format_brief():
    async def run():
        engine, factory = await _make_factory()
        try:
            sid = str(uuid.uuid4())
            await _put(
                factory,
                SeriesIn(id=sid, name="Coconut Chronicles", format_brief="Open on a face."),
            )

            # Exactly the body the un-redeployed frontend sends: no format_brief key.
            await _put(
                factory,
                SeriesIn(
                    id=sid,
                    name="Coconut Chronicles II",
                    description="renamed",
                    concept="c",
                    metadata={},
                ),
            )
            row = await _get(factory, sid)
            assert row.name == "Coconut Chronicles II"
            assert row.format_brief == "Open on a face."

            # An explicit null is "not mentioned" too: the column is NOT NULL.
            await _put(
                factory,
                SeriesIn(id=sid, name="Coconut Chronicles II", format_brief=None),
            )
            assert (await _get(factory, sid)).format_brief == "Open on a face."
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_format_brief_can_be_cleared():
    async def run():
        engine, factory = await _make_factory()
        try:
            sid = str(uuid.uuid4())
            await _put(
                factory,
                SeriesIn(id=sid, name="Coconut Chronicles", format_brief="Open on a face."),
            )
            cleared = await _put(
                factory, SeriesIn(id=sid, name="Coconut Chronicles", format_brief="")
            )
            assert cleared.format_brief == ""
            assert (await _get(factory, sid)).format_brief == ""
        finally:
            await engine.dispose()

    asyncio.run(run())


# ── migration ────────────────────────────────────────────────────────────────


class _Buffer:
    """Collects alembic's offline DDL instead of printing it to stdout."""

    def __init__(self, sink: list[str]) -> None:
        self._sink = sink

    def write(self, value: str) -> None:
        self._sink.append(value)

    def flush(self) -> None:  # pragma: no cover - trivial
        return None


def _load_migration_module():
    spec = importlib.util.spec_from_file_location(
        "migration_0041_series_format_brief", MIGRATION_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _render(fn) -> str:
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy.dialects import postgresql

    statements: list[str] = []
    ctx = MigrationContext.configure(
        dialect=postgresql.dialect(),
        opts={"as_sql": True, "output_buffer": _Buffer(statements)},
    )
    with Operations.context(ctx):
        fn()
    return " ".join(statements)


def test_migration_0041_chains_onto_0040():
    module = _load_migration_module()
    assert module.revision == "0041"
    assert module.down_revision == "0040"


def test_migration_0041_renders_the_postgres_ddl():
    module = _load_migration_module()
    # The full clause, not just the type: dropping the server_default would
    # leave ADD COLUMN … TEXT NOT NULL, which fails on a non-empty table.
    sql = _render(module.upgrade)
    assert "ALTER TABLE series ADD COLUMN format_brief TEXT DEFAULT '' NOT NULL" in sql

    assert "ALTER TABLE series DROP COLUMN format_brief" in _render(module.downgrade)
