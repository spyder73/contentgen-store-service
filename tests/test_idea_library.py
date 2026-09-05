"""Idea library + clip ratings.

Ideas are captured silently at run start; the rating lives on the clip (that
is where judgment happens) and links back via a nullable idea_id resolved at
rating time. With single-shot ideas the clip's rating IS the idea's score —
the AVG only matters so the rare rerun degrades gracefully.
"""
from __future__ import annotations

import asyncio
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, event, select
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.exc import IntegrityError
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
from app.models import ClipPrompt, ClipRating, Idea, User  # noqa: E402
from app.schemas import ClipRatingIn, IdeaIn  # noqa: E402
from app.stores import clip_ratings as ratings_store  # noqa: E402
from app.stores import ideas as ideas_store  # noqa: E402

USER_A = str(uuid.uuid4())
USER_B = str(uuid.uuid4())


async def _make_factory():
    engine = create_async_engine("sqlite+aiosqlite://")

    # sqlite ignores foreign keys unless asked per connection; 0032's ON
    # DELETE CASCADE is load-bearing (a deleted account must not leave ideas
    # or verdicts behind), so the tests exercise it for real.
    @event.listens_for(engine.sync_engine, "connect")
    def _enable_foreign_keys(dbapi_conn, _record):  # pragma: no cover - trivial
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

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


def test_rating_someone_elses_clip_is_rejected():
    """clip_id is globally unique — a forged rating would permanently block
    the real owner from rating their own clip, so the store fails closed."""

    async def run():
        engine, factory = await _make_factory()
        clip_id = await _clip(factory, USER_A)
        async with factory() as s:
            try:
                await ratings_store.upsert_rating(
                    s, USER_B, ClipRatingIn(clip_id=clip_id, score=5, note="not mine")
                )
                raise AssertionError("expected ClipRatingError")
            except ratings_store.ClipRatingError as exc:
                assert exc.status_code == 404
        # And the real owner can still rate it.
        async with factory() as s:
            mine = await ratings_store.upsert_rating(
                s, USER_A, ClipRatingIn(clip_id=clip_id, score=4, note="mine")
            )
            assert mine.score == 4
        await engine.dispose()

    asyncio.run(run())


def test_forged_idea_link_is_dropped_and_never_leaks():
    """A rating pointed at another user's idea is stored UNLINKED, and the
    idea owner's list never surfaces a foreign rating."""

    async def run():
        engine, factory = await _make_factory()
        async with factory() as s:
            ideas_of_a = await ideas_store.create_idea(s, USER_A, _idea())
        clip_of_b = await _clip(factory, USER_B)
        async with factory() as s:
            forged = await ratings_store.upsert_rating(
                s, USER_B, ClipRatingIn(clip_id=clip_of_b, score=1, note="sabotage", idea_id=ideas_of_a.id)
            )
            assert forged.idea_id is None  # the link was dropped, not stored
        async with factory() as s:
            rows = await ideas_store.list_ideas(s, USER_A)
            assert rows[0]["score"] is None and rows[0]["note"] is None
        await engine.dispose()

    asyncio.run(run())


def test_unowned_clip_is_ratable():
    """Migration 0007 added clip_prompts.user_id with no backfill, so every
    pre-multi-tenancy clip has NULL. The Go backend treats an empty owner as
    accessible (only a DIFFERENT non-empty owner is refused) and the store
    must agree, or those clips could never be rated at all."""

    async def run():
        engine, factory = await _make_factory()
        orphan = await _clip(factory, None)
        async with factory() as s:
            row = await ratings_store.upsert_rating(
                s, USER_A, ClipRatingIn(clip_id=orphan, score=3, note="legacy clip")
            )
            assert row.score == 3 and row.user_id == USER_A
        # An owned clip is still refused for everyone but its owner.
        owned = await _clip(factory, USER_A)
        async with factory() as s:
            try:
                await ratings_store.upsert_rating(
                    s, USER_B, ClipRatingIn(clip_id=owned, score=5, note="not mine")
                )
                raise AssertionError("expected ClipRatingError")
            except ratings_store.ClipRatingError as exc:
                assert exc.status_code == 404
        await engine.dispose()

    asyncio.run(run())


def test_upsert_survives_the_unique_clip_race():
    """Star click and note blur can post within milliseconds of each other:
    both read "no rating yet", both INSERT, and the loser trips
    UNIQUE(clip_id). That must update the winner, not 500."""

    async def run():
        engine, factory = await _make_factory()
        clip_id = await _clip(factory, USER_A)
        async with factory() as s:
            winner = await ratings_store.upsert_rating(
                s, USER_A, ClipRatingIn(clip_id=clip_id, score=5, note="star click")
            )

        real_get_rating = ratings_store.get_rating
        calls = {"n": 0}

        async def _blind_on_first_read(session, user_id, cid):
            # The loser's read happened before the winner committed.
            calls["n"] += 1
            if calls["n"] == 1:
                return None
            return await real_get_rating(session, user_id, cid)

        async with factory() as s:
            with patch.object(ratings_store, "get_rating", _blind_on_first_read):
                loser = await ratings_store.upsert_rating(
                    s, USER_A, ClipRatingIn(clip_id=clip_id, score=2, note="note blur")
                )
        assert calls["n"] == 2  # the INSERT really did collide
        assert loser.id == winner.id
        assert loser.score == 2 and loser.note == "note blur"

        async with factory() as s:
            rows = (
                (await s.execute(select(ClipRating).where(ClipRating.clip_id == clip_id)))
                .scalars()
                .all()
            )
            assert len(rows) == 1 and rows[0].score == 2
        await engine.dispose()

    asyncio.run(run())


def test_create_idea_is_idempotent_per_run():
    """One run == one idea row. A duplicate CreateIdea (retry, double-fire at
    run start) must return the row already minted, or GetIdeaByRunID,
    PatchIdeaRefined and the rating link would each pick a different row."""

    async def run():
        engine, factory = await _make_factory()
        run_id = str(uuid.uuid4())
        async with factory() as s:
            first = await ideas_store.create_idea(
                s, USER_A, _idea(seed="first capture", run_id=run_id)
            )
            second = await ideas_store.create_idea(
                s, USER_A, _idea(seed="typed again", run_id=run_id)
            )
            assert second.id == first.id
            assert second.seed == "first capture"  # the silent capture wins
        async with factory() as s:
            rows = (
                (await s.execute(select(Idea).where(Idea.run_id == run_id, Idea.user_id == USER_A)))
                .scalars()
                .all()
            )
            assert len(rows) == 1
            # Another account's idea for the same run id is a separate row.
            other = await ideas_store.create_idea(s, USER_B, _idea(run_id=run_id))
            assert other.id != first.id
        await engine.dispose()

    asyncio.run(run())


def test_duplicate_idea_per_run_is_refused_by_the_db():
    """The idempotent read-then-insert is racy on its own; the unique index
    from 0032 is what actually guarantees one idea row per run."""

    async def run():
        engine, factory = await _make_factory()
        run_id = str(uuid.uuid4())
        async with factory() as s:
            await ideas_store.create_idea(s, USER_A, _idea(run_id=run_id))
        async with factory() as s:
            s.add(
                Idea(
                    id=str(uuid.uuid4()),
                    user_id=USER_A,
                    seed="duplicate",
                    template_id="tpl-1",
                    template_name="",
                    params={},
                    run_id=run_id,
                )
            )
            with pytest.raises(IntegrityError):
                await s.commit()
        await engine.dispose()

    asyncio.run(run())


def test_one_rating_per_clip():
    """UNIQUE(clip_id): the clip carries exactly one verdict, ever."""

    async def run():
        engine, factory = await _make_factory()
        clip_id = await _clip(factory, USER_A)
        async with factory() as s:
            s.add(
                ClipRating(id=str(uuid.uuid4()), user_id=USER_A, clip_id=clip_id, score=5, note="a")
            )
            s.add(
                ClipRating(id=str(uuid.uuid4()), user_id=USER_B, clip_id=clip_id, score=1, note="b")
            )
            with pytest.raises(IntegrityError):
                await s.commit()
        await engine.dispose()

    asyncio.run(run())


def test_score_out_of_range_is_refused_by_the_db():
    """Pydantic guards the route; the CHECK guards everything else."""

    async def run():
        engine, factory = await _make_factory()
        clip_id = await _clip(factory, USER_A)
        async with factory() as s:
            s.add(
                ClipRating(id=str(uuid.uuid4()), user_id=USER_A, clip_id=clip_id, score=0, note="")
            )
            with pytest.raises(IntegrityError):
                await s.commit()
        await engine.dispose()

    asyncio.run(run())


def test_cascade_on_user_delete():
    """Deleting the account takes its ideas and its verdicts with it."""

    async def run():
        engine, factory = await _make_factory()
        async with factory() as s:
            idea = await ideas_store.create_idea(s, USER_A, _idea())
        clip_id = await _clip(factory, USER_A)
        async with factory() as s:
            await ratings_store.upsert_rating(
                s, USER_A, ClipRatingIn(clip_id=clip_id, score=5, note="keep", idea_id=idea.id)
            )
        async with factory() as s:
            await s.execute(delete(User).where(User.id == USER_A))
            await s.commit()
        async with factory() as s:
            assert (await s.execute(select(Idea))).scalars().all() == []
            assert (await s.execute(select(ClipRating))).scalars().all() == []
        await engine.dispose()

    asyncio.run(run())


def test_delete_idea_owner_scoping_and_rating_survives_unlinked():
    """Delete only succeeds for the owner; a surviving clip rating loses its
    idea_id (0032's FK is ON DELETE SET NULL) rather than being deleted."""

    async def run():
        engine, factory = await _make_factory()
        async with factory() as s:
            idea = await ideas_store.create_idea(s, USER_A, _idea())
        clip_id = await _clip(factory, USER_A)
        async with factory() as s:
            rating = await ratings_store.upsert_rating(
                s, USER_A, ClipRatingIn(clip_id=clip_id, score=5, note="payoff landed", idea_id=idea.id)
            )
        async with factory() as s:
            # Foreign delete is a no-op: 404 upstream, row untouched.
            assert await ideas_store.delete_idea(s, idea.id, USER_B) is False
        async with factory() as s:
            assert await ideas_store.get_idea(s, idea.id, USER_A) is not None

        async with factory() as s:
            assert await ideas_store.delete_idea(s, idea.id, USER_A) is True
        async with factory() as s:
            assert await ideas_store.get_idea(s, idea.id, USER_A) is None
            refreshed = await s.get(ClipRating, rating.id)
            assert refreshed is not None
            assert refreshed.idea_id is None
        await engine.dispose()

    asyncio.run(run())


def test_delete_idea_nonexistent_is_a_no_op():
    async def run():
        engine, factory = await _make_factory()
        async with factory() as s:
            assert await ideas_store.delete_idea(s, str(uuid.uuid4()), USER_A) is False
        await engine.dispose()

    asyncio.run(run())


def test_min_score_zero_is_not_a_filter():
    """0 stars is not a rating - the "any score" default sends 0, which must
    not hide every unrated idea."""

    async def run():
        engine, factory = await _make_factory()
        async with factory() as s:
            await ideas_store.create_idea(s, USER_A, _idea())
        async with factory() as s:
            assert len(await ideas_store.list_ideas(s, USER_A, min_score=0)) == 1
            assert len(await ideas_store.list_ideas(s, USER_A, min_score=None)) == 1
            assert await ideas_store.list_ideas(s, USER_A, min_score=1) == []
        await engine.dispose()

    asyncio.run(run())


def test_model_indexes_match_migration_0032():
    """Model/migration drift means a future autogenerate "fixes" production."""

    source = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "0032_ideas_and_clip_ratings.py"
    ).read_text()

    def _declared(table: str) -> set[str]:
        return set(re.findall(r'op\.create_index\(\s*"([^"]+)",\s*"%s"' % table, source))

    assert {ix.name for ix in Idea.__table__.indexes} == _declared("ideas")
    assert {ix.name for ix in ClipRating.__table__.indexes} == _declared("clip_ratings")
    assert {ix.name for ix in Idea.__table__.indexes if ix.unique} == {"ux_ideas_user_run"}


# ── routes ──────────────────────────────────────────────────────────────────
#
# Same harness as the other route tests: a TestClient over the real FastAPI
# app with a MagicMock session, so the wiring (headers, response models,
# error translation) is exercised without a database.

SECRET = "ideas-test-secret"


async def _session_dep():
    yield MagicMock()


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_SECRET", SECRET)
    app = create_fastapi_app()
    app.dependency_overrides[get_session] = _session_dep
    with TestClient(app) as value:
        yield value


def _headers(user_id: str = USER_A) -> dict:
    return {"X-Internal-Secret": SECRET, "X-User-ID": user_id}


def _idea_row(user_id: str = USER_A, refined=None, run_id=None) -> Idea:
    row = Idea(
        id=str(uuid.uuid4()),
        user_id=user_id,
        seed="villain sells pebbles",
        refined=refined,
        template_id="tpl-1",
        template_name="fL2TungTung",
        params={},
        run_id=run_id or str(uuid.uuid4()),
    )
    row.created_at = datetime.now(timezone.utc)
    return row


def _rating_row(clip_id: str, score: int = 4) -> ClipRating:
    row = ClipRating(
        id=str(uuid.uuid4()), user_id=USER_A, clip_id=clip_id, idea_id=None, score=score, note="n"
    )
    row.created_at = row.updated_at = datetime.now(timezone.utc)
    return row


def test_idea_routes_require_the_internal_secret(client):
    idea_id = str(uuid.uuid4())
    assert client.post("/v1/ideas", json={}).status_code == 401
    assert client.get("/v1/ideas").status_code == 401
    assert client.get(f"/v1/ideas/{idea_id}").status_code == 401
    assert client.patch(f"/v1/ideas/{idea_id}", json={"refined": "x"}).status_code == 401
    assert client.post("/v1/clip-ratings", json={}).status_code == 401
    assert client.get("/v1/clip-ratings", params={"clip_id": idea_id}).status_code == 401


def test_idea_routes_require_a_user_id(client):
    headers = {"X-Internal-Secret": SECRET}
    assert client.get("/v1/ideas", headers=headers).status_code == 401
    rating = client.get(
        "/v1/clip-ratings", params={"clip_id": str(uuid.uuid4())}, headers=headers
    )
    assert rating.status_code == 401


def test_create_idea_route_returns_idea_out(client):
    row = _idea_row()
    body = {
        "seed": row.seed,
        "template_id": row.template_id,
        "template_name": row.template_name,
        "params": {},
        "run_id": row.run_id,
    }
    with patch("app.stores.ideas.create_idea", new=AsyncMock(return_value=row)) as call:
        response = client.post("/v1/ideas", headers=_headers(), json=body)
    assert response.status_code == 200
    assert response.json()["id"] == row.id
    assert response.json()["score"] is None
    assert call.call_args.args[1] == USER_A  # the header id, never the body


def test_list_ideas_by_run_returns_zero_or_one(client):
    run_id = str(uuid.uuid4())
    with patch("app.stores.ideas.list_ideas", new=AsyncMock(return_value=[])) as call:
        empty = client.get("/v1/ideas", params={"run_id": run_id}, headers=_headers())
    assert empty.status_code == 200 and empty.json() == []
    assert call.call_args.kwargs["run_id"] == run_id

    row = _idea_row(run_id=run_id)
    merged = [
        {
            "id": row.id,
            "user_id": USER_A,
            "seed": row.seed,
            "refined": None,
            "template_id": row.template_id,
            "template_name": row.template_name,
            "params": {},
            "run_id": run_id,
            "created_at": row.created_at,
            "score": 4.0,
            "note": "landed",
        }
    ]
    with patch("app.stores.ideas.list_ideas", new=AsyncMock(return_value=merged)):
        one = client.get("/v1/ideas", params={"run_id": run_id}, headers=_headers())
    assert one.status_code == 200
    assert [i["id"] for i in one.json()] == [row.id]
    assert one.json()[0]["score"] == 4.0


def test_get_idea_route_is_404_for_another_owner(client):
    with patch("app.stores.ideas.get_idea", new=AsyncMock(return_value=None)):
        response = client.get(f"/v1/ideas/{uuid.uuid4()}", headers=_headers(USER_B))
    assert response.status_code == 404
    assert response.json()["detail"] == "idea_not_found"


def test_patch_refined_route(client):
    row = _idea_row(refined="THE FULL BRIEF")
    with patch("app.stores.ideas.patch_refined", new=AsyncMock(return_value=row)) as call:
        response = client.patch(
            f"/v1/ideas/{row.id}", headers=_headers(), json={"refined": "THE FULL BRIEF"}
        )
    assert response.status_code == 200
    assert response.json()["refined"] == "THE FULL BRIEF"
    assert call.call_args.args[3] == "THE FULL BRIEF"

    with patch("app.stores.ideas.patch_refined", new=AsyncMock(return_value=None)):
        missing = client.patch(
            f"/v1/ideas/{uuid.uuid4()}", headers=_headers(), json={"refined": "x"}
        )
    assert missing.status_code == 404


def test_delete_idea_route(client):
    idea_id = str(uuid.uuid4())
    with patch("app.stores.ideas.delete_idea", new=AsyncMock(return_value=True)) as call:
        response = client.delete(f"/v1/ideas/{idea_id}", headers=_headers())
    assert response.status_code == 204
    assert response.content == b""
    assert call.call_args.args[1:] == (idea_id, USER_A)

    with patch("app.stores.ideas.delete_idea", new=AsyncMock(return_value=False)):
        missing = client.delete(f"/v1/ideas/{idea_id}", headers=_headers())
    assert missing.status_code == 404
    assert missing.json()["detail"] == "idea_not_found"


def test_delete_idea_route_requires_the_internal_secret_and_user_id(client):
    idea_id = str(uuid.uuid4())
    assert client.delete(f"/v1/ideas/{idea_id}").status_code == 401
    assert (
        client.delete(f"/v1/ideas/{idea_id}", headers={"X-Internal-Secret": SECRET}).status_code
        == 401
    )


def test_clip_rating_routes(client):
    clip_id = str(uuid.uuid4())
    row = _rating_row(clip_id, score=4)
    with patch("app.stores.clip_ratings.upsert_rating", new=AsyncMock(return_value=row)):
        response = client.post(
            "/v1/clip-ratings",
            headers=_headers(),
            json={"clip_id": clip_id, "score": 4, "note": "n"},
        )
    assert response.status_code == 200
    assert response.json()["clip_id"] == clip_id and response.json()["score"] == 4

    # 1-5 only, refused before the store is ever reached.
    out_of_range = client.post(
        "/v1/clip-ratings", headers=_headers(), json={"clip_id": clip_id, "score": 6}
    )
    assert out_of_range.status_code == 422

    with patch(
        "app.stores.clip_ratings.upsert_rating",
        new=AsyncMock(side_effect=ratings_store.ClipRatingError(404, "clip_not_found")),
    ):
        foreign = client.post(
            "/v1/clip-ratings", headers=_headers(USER_B), json={"clip_id": clip_id, "score": 5}
        )
    assert foreign.status_code == 404 and foreign.json()["detail"] == "clip_not_found"


def test_get_clip_rating_unrated_is_200_null(client):
    clip_id = str(uuid.uuid4())
    with patch("app.stores.clip_ratings.get_rating", new=AsyncMock(return_value=None)):
        response = client.get("/v1/clip-ratings", params={"clip_id": clip_id}, headers=_headers())
    assert response.status_code == 200
    assert response.json() is None

    with patch(
        "app.stores.clip_ratings.get_rating", new=AsyncMock(return_value=_rating_row(clip_id, 5))
    ):
        rated = client.get("/v1/clip-ratings", params={"clip_id": clip_id}, headers=_headers())
    assert rated.status_code == 200 and rated.json()["score"] == 5
