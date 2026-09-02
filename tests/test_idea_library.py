"""Idea library + clip ratings.

Ideas are captured silently at run start; the rating lives on the clip (that
is where judgment happens) and links back via a nullable idea_id resolved at
rating time. With single-shot ideas the clip's rating IS the idea's score —
the AVG only matters so the rare rerun degrades gracefully.
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


from app.models import ClipPrompt, ClipRating, Idea, User  # noqa: E402
from app.schemas import ClipRatingIn, IdeaIn  # noqa: E402
from app.stores import clip_ratings as ratings_store  # noqa: E402
from app.stores import ideas as ideas_store  # noqa: E402

USER_A = str(uuid.uuid4())
USER_B = str(uuid.uuid4())


async def _make_factory():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        for table in (User.__table__, Idea.__table__, ClipPrompt.__table__, ClipRating.__table__):
            await conn.run_sync(lambda c, t=table: t.create(c))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        s.add(User(id=USER_A, username="a", password_hash="x"))
        s.add(User(id=USER_B, username="b", password_hash="x"))
        await s.commit()
    return engine, factory


def _idea(seed="villain sells pebbles", template_id="tpl-1", run_id=None):
    return IdeaIn(
        seed=seed,
        template_id=template_id,
        template_name="fL2TungTung",
        params={"target_length": "90"},
        run_id=run_id or str(uuid.uuid4()),
    )


async def _clip(factory, user_id):
    clip_id = str(uuid.uuid4())
    async with factory() as s:
        s.add(ClipPrompt(id=clip_id, user_id=user_id))
        await s.commit()
    return clip_id


def test_idea_create_list_roundtrip_with_run_lookup():
    async def run():
        engine, factory = await _make_factory()
        run_id = str(uuid.uuid4())
        async with factory() as s:
            created = await ideas_store.create_idea(s, USER_A, _idea(run_id=run_id))
        async with factory() as s:
            rows = await ideas_store.list_ideas(s, USER_A)
            assert [r["id"] for r in rows] == [created.id]
            assert rows[0]["score"] is None and rows[0]["refined"] is None
            by_run = await ideas_store.list_ideas(s, USER_A, run_id=run_id)
            assert [r["id"] for r in by_run] == [created.id]
            assert await ideas_store.list_ideas(s, USER_A, run_id=str(uuid.uuid4())) == []
        await engine.dispose()

    asyncio.run(run())


def test_patch_refined_and_owner_scoping():
    async def run():
        engine, factory = await _make_factory()
        async with factory() as s:
            created = await ideas_store.create_idea(s, USER_A, _idea())
        async with factory() as s:
            assert await ideas_store.patch_refined(s, created.id, USER_B, "stolen") is None
            patched = await ideas_store.patch_refined(s, created.id, USER_A, "THE FULL BRIEF")
            assert patched is not None and patched.refined == "THE FULL BRIEF"
        # User B's list never contains user A's ideas — the load-bearing test.
        async with factory() as s:
            assert await ideas_store.list_ideas(s, USER_B) == []
        await engine.dispose()

    asyncio.run(run())


def test_rating_upsert_is_one_row_per_clip_and_score_note_join():
    async def run():
        engine, factory = await _make_factory()
        async with factory() as s:
            idea = await ideas_store.create_idea(s, USER_A, _idea())
        clip_id = await _clip(factory, USER_A)

        async with factory() as s:
            first = await ratings_store.upsert_rating(
                s, USER_A, ClipRatingIn(clip_id=clip_id, score=5, note="payoff landed", idea_id=idea.id)
            )
            second = await ratings_store.upsert_rating(
                s, USER_A, ClipRatingIn(clip_id=clip_id, score=4, note="on rewatch: middle sags")
            )
            assert second.id == first.id  # updated in place, one row per clip
            assert second.score == 4
            # A re-rate that could not re-resolve (idea_id=None) keeps the link.
            assert second.idea_id == idea.id

        async with factory() as s:
            rows = await ideas_store.list_ideas(s, USER_A)
            assert rows[0]["score"] == 4
            assert rows[0]["note"] == "on rewatch: middle sags"
            # min_score filters on the joined value.
            assert await ideas_store.list_ideas(s, USER_A, min_score=4) != []
            assert await ideas_store.list_ideas(s, USER_A, min_score=4.5) == []
        await engine.dispose()

    asyncio.run(run())


def test_template_filter_scopes_layer3_examples():
    async def run():
        engine, factory = await _make_factory()
        async with factory() as s:
            await ideas_store.create_idea(s, USER_A, _idea(seed="reel idea", template_id="tpl-reel"))
            await ideas_store.create_idea(s, USER_A, _idea(seed="quote idea", template_id="tpl-quote"))
        async with factory() as s:
            rows = await ideas_store.list_ideas(s, USER_A, template_id="tpl-reel")
            assert [r["seed"] for r in rows] == ["reel idea"]
        await engine.dispose()

    asyncio.run(run())


def test_rerun_average_degrades_gracefully():
    """The rare rerun: two rated clips of one idea → a mean, not corruption."""

    async def run():
        engine, factory = await _make_factory()
        async with factory() as s:
            idea = await ideas_store.create_idea(s, USER_A, _idea())
        clip_1 = await _clip(factory, USER_A)
        clip_2 = await _clip(factory, USER_A)
        async with factory() as s:
            await ratings_store.upsert_rating(
                s, USER_A, ClipRatingIn(clip_id=clip_1, score=5, note="great", idea_id=idea.id)
            )
            await ratings_store.upsert_rating(
                s, USER_A, ClipRatingIn(clip_id=clip_2, score=3, note="rerun was weaker", idea_id=idea.id)
            )
        async with factory() as s:
            rows = await ideas_store.list_ideas(s, USER_A)
            assert rows[0]["score"] == 4.0
            assert rows[0]["note"] == "rerun was weaker"  # latest verdict wins
        await engine.dispose()

    asyncio.run(run())
