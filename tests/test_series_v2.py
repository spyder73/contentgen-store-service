"""Series v2 storage: template binding, cast fields, episode ledger.

Migration 0036 turns the three legacy series tables into the backbone of a
running show:

  * ``series``     gains the binding to a pipeline template (``template_id``),
    the durable show rules (``memories``), the per-checkpoint input wiring
    (``slot_map``) and the free parameter bag (``parameters``).
  * ``characters`` becomes the cast sheet: a row is a ``kind`` of
    character/place/prop, carries identity ``anchors`` and may point at a
    voice sample in ``media_items``.
  * ``episodes``   becomes the ledger of what actually ran: ``status``,
    ``run_id``, ``idea_id``, ``clip_id``, the ``storyline`` blob and the
    closing frame (``last_frame_media_id``). The old frontend stashed
    status/run_id/clip_id inside ``metadata``; the migration lifts them into
    the real columns and leaves ``metadata`` alone.
  * ``voice_snippets`` — never written by anything — is dropped.

DB-level assertions run against in-memory sqlite over the real ORM tables
(mirroring test_media_lineage.py); the migration's own DDL is asserted by
rendering it offline in the postgres dialect (mirroring test_user_settings.py).
"""
from __future__ import annotations

import asyncio
import importlib.util
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import delete, event, text
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
from app.models import (  # noqa: E402
    Character,
    ClipPrompt,
    Episode,
    MediaItem,
    Series,
    User,
)
from app.schemas import (  # noqa: E402
    CharacterIn,
    EpisodeIn,
    EpisodeOut,
    EpisodePatch,
    SeriesIn,
)
from app.stores import characters as characters_store  # noqa: E402
from app.stores import episodes as episodes_store  # noqa: E402
from app.stores import series as series_store  # noqa: E402

USER_A = str(uuid.uuid4())

MIGRATION_PATH = (
    Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0036_series_v2.py"
)


async def _make_factory():
    engine = create_async_engine("sqlite+aiosqlite://")

    # ON DELETE SET NULL on voice_media_id / last_frame_media_id is the point of
    # two of these tests, and sqlite ignores foreign keys unless asked.
    @event.listens_for(engine.sync_engine, "connect")
    def _enable_foreign_keys(dbapi_conn, _record):  # pragma: no cover - trivial
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with engine.begin() as conn:
        for table in (
            User.__table__,
            ClipPrompt.__table__,
            MediaItem.__table__,
            Series.__table__,
            Character.__table__,
            Episode.__table__,
        ):
            await conn.run_sync(lambda c, t=table: t.create(c))
        # characters.generator_profile_id references it and sqlite refuses to
        # insert against a missing table even for a NULL value; the real
        # generator_profiles DDL carries postgres-only defaults, so a stand-in
        # with just the referenced key is enough here.
        await conn.execute(
            text("CREATE TABLE generator_profiles (id VARCHAR(36) PRIMARY KEY)")
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        s.add(User(id=USER_A, username="a", password_hash="x"))
        await s.commit()
    return engine, factory


async def _add_media(factory, media_id: str) -> None:
    async with factory() as s:
        s.add(
            MediaItem(
                id=media_id,
                user_id=USER_A,
                type="audio",
                prompt="p",
                file_url=f"/media/{media_id}.wav",
                metadata_={},
            )
        )
        await s.commit()


async def _seed_series(factory, series_id: str) -> None:
    async with factory() as s:
        await series_store.upsert_series(
            s, SeriesIn(id=series_id, name="Coconut Chronicles"), user_id=USER_A
        )


# ── series ───────────────────────────────────────────────────────────────────


def test_series_v2_columns_roundtrip():
    """template_id / memories / slot_map / parameters survive a write-read."""

    async def run():
        engine, factory = await _make_factory()
        try:
            sid, cid, mid = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
            memories = [
                {
                    "id": "mem-1",
                    "text": "the coconut is never found on screen",
                    "created_at": "2026-09-13T10:00:00Z",
                    "source": "user",
                }
            ]
            slot_map = {
                "checkpoint:hero_frame": [
                    {"source": "cast", "character_id": cid},
                    {"source": "media", "media_id": mid},
                    {"source": "episode:last_frame"},
                ]
            }
            body = SeriesIn(
                id=sid,
                name="Coconut Chronicles",
                concept="a pirate who lost his coconut",
                template_id="reel-v3",
                memories=memories,
                slot_map=slot_map,
                parameters={"tone": "dry", "scene_count": "4"},
            )
            async with factory() as s:
                written = await series_store.upsert_series(s, body, user_id=USER_A)
            assert written.template_id == "reel-v3"

            async with factory() as s:
                row = await series_store.get_series(s, sid, user_id=USER_A)
            assert row is not None
            assert row.template_id == "reel-v3"
            assert row.memories == memories
            assert row.slot_map == slot_map
            assert row.parameters == {"tone": "dry", "scene_count": "4"}
            # The concept text stays where it was — there is no free-text bible.
            assert row.concept == "a pirate who lost his coconut"
        finally:
            await engine.dispose()

    asyncio.run(run())

    # A memory with no usable text is a rule nothing can render: rejected at
    # the schema layer, so the route answers 422 instead of storing noise.
    with pytest.raises(ValidationError):
        SeriesIn(id=str(uuid.uuid4()), name="n", memories=[{"id": "mem-1"}])
    with pytest.raises(ValidationError):
        SeriesIn(id=str(uuid.uuid4()), name="n", memories=[{"text": "   "}])
    with pytest.raises(ValidationError):
        SeriesIn(id=str(uuid.uuid4()), name="n", memories=["just a string"])


def test_series_v2_columns_default_empty():
    """Rows written by clients that know nothing of v2 read back as empty."""

    async def run():
        engine, factory = await _make_factory()
        try:
            sid = str(uuid.uuid4())
            await _seed_series(factory, sid)
            async with factory() as s:
                row = await series_store.get_series(s, sid, user_id=USER_A)
            assert row is not None
            assert row.template_id is None
            assert row.memories == []
            assert row.slot_map == {}
            assert row.parameters == {}
        finally:
            await engine.dispose()

    asyncio.run(run())


# ── characters (the cast sheet) ──────────────────────────────────────────────


def test_character_kind_anchors_voice():
    """kind/anchors round-trip, and a deleted voice sample nulls the FK."""

    async def run():
        engine, factory = await _make_factory()
        try:
            sid, cid, voice_id = (
                str(uuid.uuid4()),
                str(uuid.uuid4()),
                str(uuid.uuid4()),
            )
            await _seed_series(factory, sid)
            await _add_media(factory, voice_id)

            anchors = [{"label": "sun-bleached beach"}, {"label": "one palm tree"}]
            async with factory() as s:
                await characters_store.upsert_character(
                    s,
                    CharacterIn(
                        id=cid,
                        series_id=sid,
                        name="The Beach",
                        kind="place",
                        anchors=anchors,
                        voice_media_id=voice_id,
                    ),
                )

            async with factory() as s:
                row = await characters_store.get_character(s, cid, user_id=USER_A)
            assert row is not None
            assert row.kind == "place"
            assert row.anchors == anchors
            assert row.voice_media_id == voice_id

            # Deleting the sample must not orphan the cast row.
            async with factory() as s:
                await s.execute(delete(MediaItem).where(MediaItem.id == voice_id))
                await s.commit()

            async with factory() as s:
                row = await characters_store.get_character(s, cid, user_id=USER_A)
            assert row is not None
            assert row.voice_media_id is None
            assert row.kind == "place"
        finally:
            await engine.dispose()

    asyncio.run(run())

    # Only the three cast kinds exist; anything else is a typo, not a feature.
    with pytest.raises(ValidationError):
        CharacterIn(id=str(uuid.uuid4()), series_id=str(uuid.uuid4()), name="n", kind="location")
    for kind in ("character", "place", "prop"):
        assert CharacterIn(
            id=str(uuid.uuid4()), series_id=str(uuid.uuid4()), name="n", kind=kind
        ).kind == kind


def test_character_defaults_to_character_kind():
    async def run():
        engine, factory = await _make_factory()
        try:
            sid, cid = str(uuid.uuid4()), str(uuid.uuid4())
            await _seed_series(factory, sid)
            async with factory() as s:
                await characters_store.upsert_character(
                    s, CharacterIn(id=cid, series_id=sid, name="Captain")
                )
            async with factory() as s:
                row = await characters_store.get_character(s, cid, user_id=USER_A)
            assert row is not None
            assert row.kind == "character"
            assert row.anchors == []
            assert row.voice_media_id is None
        finally:
            await engine.dispose()

    asyncio.run(run())


# ── episodes (the ledger) ────────────────────────────────────────────────────


def _load_migration_module():
    spec = importlib.util.spec_from_file_location("migration_0036_series_v2", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_episode_v2_columns_and_backfill():
    """The ledger columns round-trip, and legacy metadata is lifted into them."""

    async def run():
        engine, factory = await _make_factory()
        try:
            sid, eid, frame_id = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
            await _seed_series(factory, sid)
            await _add_media(factory, frame_id)

            storyline = {"beats": [{"scene": 1, "text": "the coconut rolls away"}]}
            run_id, idea_id, clip_id = (
                str(uuid.uuid4()),
                str(uuid.uuid4()),
                str(uuid.uuid4()),
            )
            async with factory() as s:
                await episodes_store.upsert_episode(
                    s,
                    EpisodeIn(
                        id=eid,
                        series_id=sid,
                        episode_number=1,
                        title="Pilot",
                        status="running",
                        run_id=run_id,
                        idea_id=idea_id,
                        clip_id=clip_id,
                        storyline=storyline,
                        last_frame_media_id=frame_id,
                    ),
                )

            async with factory() as s:
                row = await episodes_store.get_episode(s, eid, user_id=USER_A)
            assert row is not None
            assert row.status == "running"
            assert row.run_id == run_id
            assert row.idea_id == idea_id
            assert row.clip_id == clip_id
            assert row.storyline == storyline
            assert row.last_frame_media_id == frame_id

            # A deleted closing frame nulls the FK rather than orphaning the row.
            async with factory() as s:
                await s.execute(delete(MediaItem).where(MediaItem.id == frame_id))
                await s.commit()
            async with factory() as s:
                row = await episodes_store.get_episode(s, eid, user_id=USER_A)
            assert row is not None
            assert row.last_frame_media_id is None

            # Defaults for a client that knows nothing of v2.
            plain = str(uuid.uuid4())
            async with factory() as s:
                await episodes_store.upsert_episode(
                    s, EpisodeIn(id=plain, series_id=sid, episode_number=2)
                )
            async with factory() as s:
                row = await episodes_store.get_episode(s, plain, user_id=USER_A)
            assert row is not None
            assert row.status == "draft"
            assert row.run_id is None
            assert row.idea_id is None
            assert row.clip_id is None
            assert row.storyline == {}

            # ── backfill ────────────────────────────────────────────────────
            # A row as the old frontend left it: everything inside metadata.
            legacy, legacy_run, legacy_clip = (
                str(uuid.uuid4()),
                str(uuid.uuid4()),
                str(uuid.uuid4()),
            )
            legacy_metadata = {
                "status": "done",
                "run_id": legacy_run,
                "clip_id": legacy_clip,
                "note": "kept as-is",
            }
            async with factory() as s:
                s.add(
                    Episode(
                        id=legacy,
                        series_id=sid,
                        episode_number=3,
                        metadata_=legacy_metadata,
                    )
                )
                await s.commit()

            module = _load_migration_module()
            async with factory() as s:
                for statement in module.BACKFILL_STATEMENTS:
                    await s.execute(text(statement))
                await s.commit()

            async with factory() as s:
                row = await episodes_store.get_episode(s, legacy, user_id=USER_A)
            assert row is not None
            assert row.status == "done"
            assert row.run_id == legacy_run
            assert row.clip_id == legacy_clip
            # metadata is left untouched — nothing else reading it breaks.
            assert row.metadata == legacy_metadata

            # Rows with nothing to lift keep their defaults.
            async with factory() as s:
                row = await episodes_store.get_episode(s, plain, user_id=USER_A)
            assert row is not None
            assert row.status == "draft"
            assert row.run_id is None
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


def test_migration_0036_chains_onto_0035():
    module = _load_migration_module()
    assert module.revision == "0036"
    assert module.down_revision == "0035"


def test_migration_0036_renders_the_contracted_postgres_ddl():
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy.dialects import postgresql

    module = _load_migration_module()
    statements: list[str] = []
    ctx = MigrationContext.configure(
        dialect=postgresql.dialect(),
        opts={"as_sql": True, "output_buffer": _Buffer(statements)},
    )
    with Operations.context(ctx):
        module.upgrade()
    sql = " ".join(statements)

    assert "ALTER TABLE series ADD COLUMN template_id TEXT" in sql
    # The full clause, not just the type: dropping a server_default would leave
    # ADD COLUMN … JSONB NOT NULL, which fails outright on a non-empty table —
    # and this offline render is the only guard a migration with no rehearsal
    # gets.
    for table, column, default in (
        ("series", "memories", "[]"),
        ("series", "slot_map", "{}"),
        ("series", "parameters", "{}"),
        ("characters", "anchors", "[]"),
        ("episodes", "storyline", "{}"),
    ):
        assert (
            f"ALTER TABLE {table} ADD COLUMN {column} JSONB DEFAULT '{default}' NOT NULL"
            in sql
        )
    assert "ALTER TABLE characters ADD COLUMN kind TEXT DEFAULT 'character' NOT NULL" in sql
    assert "ALTER TABLE episodes ADD COLUMN status TEXT DEFAULT 'draft' NOT NULL" in sql
    for column in ("run_id", "idea_id", "clip_id"):
        assert f"ALTER TABLE episodes ADD COLUMN {column} TEXT" in sql
    # The two media references must release, not block, a media delete.
    assert sql.count("REFERENCES media_items (id) ON DELETE SET NULL") == 2
    assert "UPDATE episodes SET" in sql
    assert "DROP TABLE voice_snippets" in sql

    # The downgrade puts the dead table back so the step is reversible.
    statements.clear()
    ctx = MigrationContext.configure(
        dialect=postgresql.dialect(),
        opts={"as_sql": True, "output_buffer": _Buffer(statements)},
    )
    with Operations.context(ctx):
        module.downgrade()
    down = " ".join(statements)
    assert "CREATE TABLE voice_snippets" in down
    assert "DROP COLUMN template_id" in down
    assert "DROP COLUMN voice_media_id" in down
    assert "DROP COLUMN last_frame_media_id" in down


# ── routes ───────────────────────────────────────────────────────────────────

SECRET = "series-v2-secret"


async def _session_dep():
    yield MagicMock()


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_SECRET", SECRET)
    app = create_fastapi_app()
    app.dependency_overrides[get_session] = _session_dep
    with TestClient(app, raise_server_exceptions=True) as value:
        yield value


def _headers() -> dict[str, str]:
    return {"X-Internal-Secret": SECRET, "X-User-ID": USER_A}


def test_voice_snippet_routes_gone(client):
    """voice_snippets never carried data; its routes go with the table."""
    vid = str(uuid.uuid4())
    assert client.get("/v1/voice-snippets", headers=_headers()).status_code == 404
    assert client.get(f"/v1/voice-snippets/{vid}", headers=_headers()).status_code == 404
    assert client.delete(f"/v1/voice-snippets/{vid}", headers=_headers()).status_code == 404


def test_series_put_rejects_memory_without_text(client):
    resp = client.put(
        f"/v1/series/{uuid.uuid4()}",
        headers=_headers(),
        json={"id": str(uuid.uuid4()), "name": "n", "memories": [{"id": "mem-1"}]},
    )
    assert resp.status_code == 422


def test_character_put_rejects_unknown_kind(client):
    resp = client.put(
        f"/v1/characters/{uuid.uuid4()}",
        headers=_headers(),
        json={
            "id": str(uuid.uuid4()),
            "series_id": str(uuid.uuid4()),
            "name": "n",
            "kind": "location",
        },
    )
    assert resp.status_code == 422


# ── merge semantics: an old-shape PUT must not wipe the v2 columns ───────────
#
# The live React app keeps PUTting the v1 shape until it is redeployed, and the
# Go DTOs carry no v2 fields either. A blind overwrite would silently reset all
# thirteen columns — the status reset being the sharpest edge (a rename would
# flip a running episode back to draft). The rule: a field the caller never
# mentioned keeps its stored value; an explicitly sent [] / {} does clear.


def test_old_shape_put_preserves_series_v2_columns():
    async def run():
        engine, factory = await _make_factory()
        try:
            sid = str(uuid.uuid4())
            memories = [{"id": "mem-1", "text": "no coconut on screen"}]
            async with factory() as s:
                await series_store.upsert_series(
                    s,
                    SeriesIn(
                        id=sid,
                        name="Coconut Chronicles",
                        template_id="reel-v3",
                        memories=memories,
                        slot_map={"checkpoint:hero": [{"source": "episode:last_frame"}]},
                        parameters={"tone": "dry"},
                    ),
                    user_id=USER_A,
                )

            # Exactly the body the un-redeployed frontend sends.
            async with factory() as s:
                await series_store.upsert_series(
                    s,
                    SeriesIn(
                        id=sid,
                        name="Coconut Chronicles II",
                        description="renamed",
                        concept="c",
                        metadata={},
                    ),
                    user_id=USER_A,
                )

            async with factory() as s:
                row = await series_store.get_series(s, sid, user_id=USER_A)
            assert row is not None
            assert row.name == "Coconut Chronicles II"
            assert row.template_id == "reel-v3"
            assert row.memories == memories
            assert row.slot_map == {"checkpoint:hero": [{"source": "episode:last_frame"}]}
            assert row.parameters == {"tone": "dry"}

            # An explicit empty value IS present, and clears.
            async with factory() as s:
                await series_store.upsert_series(
                    s,
                    SeriesIn(
                        id=sid,
                        name="Coconut Chronicles II",
                        memories=[],
                        slot_map={},
                        parameters={},
                    ),
                    user_id=USER_A,
                )
            async with factory() as s:
                row = await series_store.get_series(s, sid, user_id=USER_A)
            assert row is not None
            assert row.memories == []
            assert row.slot_map == {}
            assert row.parameters == {}
            assert row.template_id == "reel-v3"  # still never mentioned
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_old_shape_put_preserves_character_v2_columns():
    async def run():
        engine, factory = await _make_factory()
        try:
            sid, cid, voice_id = (
                str(uuid.uuid4()),
                str(uuid.uuid4()),
                str(uuid.uuid4()),
            )
            await _seed_series(factory, sid)
            await _add_media(factory, voice_id)
            anchors = [{"label": "one palm tree"}]
            async with factory() as s:
                await characters_store.upsert_character(
                    s,
                    CharacterIn(
                        id=cid,
                        series_id=sid,
                        name="The Beach",
                        kind="place",
                        anchors=anchors,
                        voice_media_id=voice_id,
                    ),
                )

            async with factory() as s:
                await characters_store.upsert_character(
                    s,
                    CharacterIn(id=cid, series_id=sid, name="The Beach", voice="warm"),
                )

            async with factory() as s:
                row = await characters_store.get_character(s, cid, user_id=USER_A)
            assert row is not None
            assert row.voice == "warm"
            assert row.kind == "place"
            assert row.anchors == anchors
            assert row.voice_media_id == voice_id

            async with factory() as s:
                await characters_store.upsert_character(
                    s, CharacterIn(id=cid, series_id=sid, name="The Beach", anchors=[])
                )
            async with factory() as s:
                row = await characters_store.get_character(s, cid, user_id=USER_A)
            assert row is not None
            assert row.anchors == []
            assert row.kind == "place"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_old_shape_put_preserves_episode_v2_columns():
    async def run():
        engine, factory = await _make_factory()
        try:
            sid, eid, frame_id = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
            await _seed_series(factory, sid)
            await _add_media(factory, frame_id)
            run_id, idea_id, clip_id = (
                str(uuid.uuid4()),
                str(uuid.uuid4()),
                str(uuid.uuid4()),
            )
            storyline = {"beats": ["the coconut rolls away"]}
            async with factory() as s:
                await episodes_store.upsert_episode(
                    s,
                    EpisodeIn(
                        id=eid,
                        series_id=sid,
                        episode_number=1,
                        title="Pilot",
                        status="running",
                        run_id=run_id,
                        idea_id=idea_id,
                        clip_id=clip_id,
                        storyline=storyline,
                        last_frame_media_id=frame_id,
                    ),
                )

            # A rename from the current UI: v1 shape, no ledger fields.
            async with factory() as s:
                await episodes_store.upsert_episode(
                    s,
                    EpisodeIn(
                        id=eid,
                        series_id=sid,
                        episode_number=1,
                        title="Pilot (renamed)",
                        synopsis="s",
                        prev_episode_summary="",
                        metadata={},
                    ),
                )

            async with factory() as s:
                row = await episodes_store.get_episode(s, eid, user_id=USER_A)
            assert row is not None
            assert row.title == "Pilot (renamed)"
            assert row.status == "running"
            assert row.run_id == run_id
            assert row.idea_id == idea_id
            assert row.clip_id == clip_id
            assert row.storyline == storyline
            assert row.last_frame_media_id == frame_id

            async with factory() as s:
                await episodes_store.upsert_episode(
                    s,
                    EpisodeIn(id=eid, series_id=sid, episode_number=1, storyline={}),
                )
            async with factory() as s:
                row = await episodes_store.get_episode(s, eid, user_id=USER_A)
            assert row is not None
            assert row.storyline == {}
            assert row.status == "running"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_series_put_ignores_user_id_and_timestamps():
    """Ownership and audit stamps are the store's to decide, never the body's."""
    body = SeriesIn.model_validate(
        {
            "id": str(uuid.uuid4()),
            "name": "n",
            "user_id": str(uuid.uuid4()),
            "created_at": "1999-01-01T00:00:00Z",
            "updated_at": "1999-01-01T00:00:00Z",
        }
    )
    for field in ("user_id", "created_at", "updated_at"):
        assert not hasattr(body, field)
        assert field not in body.model_dump()

    async def run():
        engine, factory = await _make_factory()
        try:
            sid, other = str(uuid.uuid4()), str(uuid.uuid4())
            async with factory() as s:
                s.add(User(id=other, username="b", password_hash="x"))
                await s.commit()
            await _seed_series(factory, sid)
            async with factory() as s:
                row = await s.get(Series, sid)
                created = row.created_at

            # A later PUT — even one carrying another user's id in the body —
            # cannot hand the show to someone else.
            async with factory() as s:
                await series_store.upsert_series(
                    s,
                    SeriesIn.model_validate(
                        {"id": sid, "name": "renamed", "user_id": other}
                    ),
                    user_id=other,
                )
            async with factory() as s:
                row = await s.get(Series, sid)
            assert row.user_id == USER_A
            assert row.created_at == created
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_series_parameters_must_be_strings():
    """Pipeline parameters are strings; a number is a second encoding."""
    with pytest.raises(ValidationError):
        SeriesIn(id=str(uuid.uuid4()), name="n", parameters={"scene_count": 4})
    with pytest.raises(ValidationError):
        SeriesIn(id=str(uuid.uuid4()), name="n", parameters={"nested": {"a": "b"}})
    assert SeriesIn(
        id=str(uuid.uuid4()), name="n", parameters={"scene_count": "4"}
    ).parameters == {"scene_count": "4"}


# ── absent vs explicit null on the nullable v2 scalars ───────────────────────
#
# The Go client forwards exactly the keys its caller sent, so the store has to
# tell "never mentioned" (leave it) from "sent as null" (clear it). Membership
# in model_fields_set is the only thing that distinguishes the two — the value
# is None either way.


def test_series_put_null_template_id_unbinds_absent_leaves():
    async def run():
        engine, factory = await _make_factory()
        try:
            sid = str(uuid.uuid4())
            async with factory() as s:
                await series_store.upsert_series(
                    s,
                    SeriesIn(id=sid, name="Coconut Chronicles", template_id="reel-v3"),
                    user_id=USER_A,
                )

            # Key absent: the binding survives.
            async with factory() as s:
                await series_store.upsert_series(
                    s,
                    SeriesIn.model_validate({"id": sid, "name": "renamed"}),
                    user_id=USER_A,
                )
            async with factory() as s:
                row = await series_store.get_series(s, sid, user_id=USER_A)
            assert row is not None
            assert row.template_id == "reel-v3"

            # Key sent as null: the show is unbound.
            async with factory() as s:
                await series_store.upsert_series(
                    s,
                    SeriesIn.model_validate(
                        {"id": sid, "name": "renamed", "template_id": None}
                    ),
                    user_id=USER_A,
                )
            async with factory() as s:
                row = await series_store.get_series(s, sid, user_id=USER_A)
            assert row is not None
            assert row.template_id is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_character_put_null_voice_media_id_unlinks_absent_leaves():
    async def run():
        engine, factory = await _make_factory()
        try:
            sid, cid, voice_id = (
                str(uuid.uuid4()),
                str(uuid.uuid4()),
                str(uuid.uuid4()),
            )
            await _seed_series(factory, sid)
            await _add_media(factory, voice_id)
            async with factory() as s:
                await characters_store.upsert_character(
                    s,
                    CharacterIn(
                        id=cid, series_id=sid, name="Captain", voice_media_id=voice_id
                    ),
                )

            async with factory() as s:
                await characters_store.upsert_character(
                    s,
                    CharacterIn.model_validate(
                        {"id": cid, "series_id": sid, "name": "Captain"}
                    ),
                )
            async with factory() as s:
                row = await characters_store.get_character(s, cid, user_id=USER_A)
            assert row is not None
            assert row.voice_media_id == voice_id

            async with factory() as s:
                await characters_store.upsert_character(
                    s,
                    CharacterIn.model_validate(
                        {
                            "id": cid,
                            "series_id": sid,
                            "name": "Captain",
                            "voice_media_id": None,
                        }
                    ),
                )
            async with factory() as s:
                row = await characters_store.get_character(s, cid, user_id=USER_A)
            assert row is not None
            assert row.voice_media_id is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_episode_null_ledger_refs_clear_via_put_and_patch():
    async def run():
        engine, factory = await _make_factory()
        try:
            sid, eid = str(uuid.uuid4()), str(uuid.uuid4())
            await _seed_series(factory, sid)
            run_id, idea_id, clip_id = (
                str(uuid.uuid4()),
                str(uuid.uuid4()),
                str(uuid.uuid4()),
            )
            base = {"id": eid, "series_id": sid, "episode_number": 1}
            async with factory() as s:
                await episodes_store.upsert_episode(
                    s,
                    EpisodeIn(
                        **base,
                        status="running",
                        run_id=run_id,
                        idea_id=idea_id,
                        clip_id=clip_id,
                    ),
                )

            # Absent: the whole ledger survives.
            async with factory() as s:
                await episodes_store.upsert_episode(
                    s, EpisodeIn.model_validate({**base, "title": "renamed"})
                )
            async with factory() as s:
                row = await episodes_store.get_episode(s, eid, user_id=USER_A)
            assert row is not None
            assert (row.run_id, row.idea_id, row.clip_id) == (run_id, idea_id, clip_id)

            # Explicit null on one reference clears only that one.
            async with factory() as s:
                await episodes_store.upsert_episode(
                    s, EpisodeIn.model_validate({**base, "run_id": None})
                )
            async with factory() as s:
                row = await episodes_store.get_episode(s, eid, user_id=USER_A)
            assert row is not None
            assert row.run_id is None
            assert row.idea_id == idea_id
            assert row.clip_id == clip_id
            assert row.status == "running"

            # Same rule through PATCH.
            async with factory() as s:
                out = await episodes_store.patch_episode(
                    s, eid, EpisodePatch.model_validate({"clip_id": None}), user_id=USER_A
                )
            assert out is not None
            assert out.clip_id is None
            assert out.idea_id == idea_id
            assert out.status == "running"

            # A PATCH that mentions nothing changes nothing.
            async with factory() as s:
                out = await episodes_store.patch_episode(
                    s, eid, EpisodePatch.model_validate({}), user_id=USER_A
                )
            assert out is not None
            assert out.idea_id == idea_id
            assert out.status == "running"
        finally:
            await engine.dispose()

    asyncio.run(run())

# ── PATCH /v1/episodes/{id} ──────────────────────────────────────────────────


def test_patch_episode_writes_only_given_keys():
    async def run():
        engine, factory = await _make_factory()
        try:
            sid, eid = str(uuid.uuid4()), str(uuid.uuid4())
            await _seed_series(factory, sid)
            run_id = str(uuid.uuid4())
            async with factory() as s:
                await episodes_store.upsert_episode(
                    s,
                    EpisodeIn(
                        id=eid,
                        series_id=sid,
                        episode_number=1,
                        title="Pilot",
                        status="running",
                        run_id=run_id,
                        storyline={"beats": ["a"]},
                        metadata={"keep": "me"},
                    ),
                )

            # The recorder knows one fact at a time.
            async with factory() as s:
                out = await episodes_store.patch_episode(
                    s,
                    eid,
                    EpisodePatch(status="complete", storyline={"beats": ["a", "b"]}),
                    user_id=USER_A,
                )
            assert out is not None
            assert out.status == "complete"
            assert out.storyline == {"beats": ["a", "b"]}
            assert out.title == "Pilot"
            assert out.run_id == run_id
            assert out.metadata == {"keep": "me"}
            assert out.episode_number == 1

            # Explicit null clears a nullable ledger reference.
            async with factory() as s:
                out = await episodes_store.patch_episode(
                    s, eid, EpisodePatch(run_id=None), user_id=USER_A
                )
            assert out is not None
            assert out.run_id is None
            assert out.status == "complete"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_patch_episode_is_owner_scoped():
    async def run():
        engine, factory = await _make_factory()
        try:
            sid, eid, other = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
            async with factory() as s:
                s.add(User(id=other, username="b", password_hash="x"))
                await s.commit()
            await _seed_series(factory, sid)
            async with factory() as s:
                await episodes_store.upsert_episode(
                    s,
                    EpisodeIn(
                        id=eid, series_id=sid, episode_number=1, status="running"
                    ),
                )

            async with factory() as s:
                assert (
                    await episodes_store.patch_episode(
                        s, eid, EpisodePatch(status="stolen"), user_id=other
                    )
                    is None
                )
                assert (
                    await episodes_store.patch_episode(
                        s, str(uuid.uuid4()), EpisodePatch(status="x"), user_id=USER_A
                    )
                    is None
                )
            async with factory() as s:
                row = await episodes_store.get_episode(s, eid, user_id=USER_A)
            assert row is not None
            assert row.status == "running"
        finally:
            await engine.dispose()

    asyncio.run(run())

    async def requires_user():
        with pytest.raises(ValueError):
            await episodes_store.patch_episode(
                MagicMock(), str(uuid.uuid4()), EpisodePatch(status="x"), user_id=None
            )

    asyncio.run(requires_user())


def test_patch_episode_route(client):
    eid = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    out = EpisodeOut(
        id=eid,
        series_id=str(uuid.uuid4()),
        episode_number=1,
        status="complete",
        created_at=now,
        updated_at=now,
    )
    with patch(
        "app.stores.episodes.patch_episode", new=AsyncMock(return_value=out)
    ) as m:
        resp = client.patch(
            f"/v1/episodes/{eid}", headers=_headers(), json={"status": "complete"}
        )
    assert resp.status_code == 200
    assert resp.json()["status"] == "complete"
    assert m.call_args.kwargs.get("user_id") == USER_A
    # Only the sent key reaches the store.
    assert m.call_args.args[2].model_dump(exclude_unset=True) == {"status": "complete"}


def test_patch_episode_route_404_when_unknown_or_foreign(client):
    with patch("app.stores.episodes.patch_episode", new=AsyncMock(return_value=None)):
        resp = client.patch(
            f"/v1/episodes/{uuid.uuid4()}", headers=_headers(), json={"status": "x"}
        )
    assert resp.status_code == 404


def test_patch_episode_route_rejects_unknown_key(client):
    resp = client.patch(
        f"/v1/episodes/{uuid.uuid4()}", headers=_headers(), json={"statuss": "complete"}
    )
    assert resp.status_code == 422
    # A null on a NOT NULL column is refused too.
    resp = client.patch(
        f"/v1/episodes/{uuid.uuid4()}", headers=_headers(), json={"status": None}
    )
    assert resp.status_code == 422


def test_patch_episode_without_user_id_401(client):
    resp = client.patch(
        f"/v1/episodes/{uuid.uuid4()}",
        headers={"X-Internal-Secret": SECRET},
        json={"status": "x"},
    )
    assert resp.status_code == 401


# ── the guarded drop ─────────────────────────────────────────────────────────


def test_migration_0036_drop_is_guarded_by_table_existence():
    """A table that is already gone must not roll back thirteen ADD COLUMNs."""
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    module = _load_migration_module()
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            assert module._voice_snippets_exists() is False
            conn.execute(sa.text("CREATE TABLE voice_snippets (id TEXT)"))
            assert module._voice_snippets_exists() is True
    engine.dispose()
