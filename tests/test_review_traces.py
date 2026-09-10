"""Reviewer trace log + human correction rulings.

``review_traces`` is an append-only log of every check-tier call the reviewer
made (one row per attempt, caller-minted id so the Go backend's own retry is
idempotent). ``review_corrections`` is the much smaller table of human
rulings on a verdict — upserted on (user_id, verdict_id) so re-judging a
verdict updates the existing ruling instead of forking it.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from unittest.mock import AsyncMock, MagicMock, patch


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(element, compiler, **kw):  # pragma: no cover - trivial
    return "JSON"


@compiles(UUID, "sqlite")
def _uuid_sqlite(element, compiler, **kw):  # pragma: no cover - trivial
    return "VARCHAR(36)"


from app.db import get_session  # noqa: E402
from app.fastapi_app import create_fastapi_app  # noqa: E402
from app.models import ReviewCorrection, ReviewTrace, User  # noqa: E402
from app.schemas import ReviewCorrectionIn, ReviewCorrectionOut, ReviewTraceIn  # noqa: E402
from app.stores import review_corrections  # noqa: E402
from app.stores import review_traces  # noqa: E402

USER_A = str(uuid.uuid4())
USER_B = str(uuid.uuid4())


async def _make_factory():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        for table in (User.__table__, ReviewTrace.__table__, ReviewCorrection.__table__):
            await conn.run_sync(lambda c, t=table: t.create(c))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        s.add(User(id=USER_A, username="a", password_hash="x"))
        s.add(User(id=USER_B, username="b", password_hash="x"))
        await s.commit()
    return engine, factory


def test_trace_create_is_idempotent_and_lists_by_run():
    async def run():
        engine, factory = await _make_factory()
        run_id = str(uuid.uuid4())
        payload = ReviewTraceIn(id=str(uuid.uuid4()), run_id=run_id, checkpoint_id="gen", checkpoint_index=2,
                                attempt=1, tier="check", system_prompt="p" * 5000, raw_output="{}", outcome="pass",
                                images=[{"label": "CANDIDATE"}], prompt_chars=5000, latency_ms=1200)
        async with factory() as s:
            a = await review_traces.create_trace(s, USER_A, payload)
            b = await review_traces.create_trace(s, USER_A, payload)
            assert a.id == b.id
        async with factory() as s:
            rows = await review_traces.list_traces(s, USER_A, run_id=run_id)
            assert [r.id for r in rows] == [a.id]
            assert rows[0].system_prompt == "p" * 5000
            assert await review_traces.list_traces(s, USER_B, run_id=run_id) == []
        await engine.dispose()
    asyncio.run(run())


def test_correction_upserts_on_verdict_and_lists_newest_first():
    async def run():
        engine, factory = await _make_factory()
        run_id, verdict_id = str(uuid.uuid4()), str(uuid.uuid4())
        first = ReviewCorrectionIn(run_id=run_id, verdict_id=verdict_id, template_id="tpl", checkpoint_id="gen",
                                   label="false_pass", reason="it passed a collage", scope="this_pipeline", source="user")
        async with factory() as s:
            c1 = await review_corrections.upsert_correction(s, USER_A, first)
            c2 = await review_corrections.upsert_correction(s, USER_A, first.model_copy(update={"label": "false_fail", "reason": "actually fine"}))
            assert c1.id == c2.id and c2.label == "false_fail"
        async with factory() as s:
            rows = await review_corrections.list_corrections(s, USER_A, template_id="tpl", limit=10)
            assert [r.id for r in rows] == [c1.id]
            assert await review_corrections.list_corrections(s, USER_B, template_id="tpl") == []
        await engine.dispose()
    asyncio.run(run())


def test_correction_rejects_bad_label():
    with pytest.raises(ValidationError):
        ReviewCorrectionIn(run_id="r", verdict_id="v", template_id="t", checkpoint_id="c", label="meh", reason="", scope="this_pipeline", source="user")


def test_correction_upsert_stores_and_returns_lesson_and_scope():
    async def run():
        engine, factory = await _make_factory()
        payload = ReviewCorrectionIn(
            run_id=str(uuid.uuid4()), verdict_id=str(uuid.uuid4()), template_id="tpl", checkpoint_id="gen",
            label="false_pass", reason="raw note", lesson="never trust a blurry hand", scope="this_checkpoint",
            source="user",
        )
        async with factory() as s:
            row = await review_corrections.upsert_correction(s, USER_A, payload)
            assert row.lesson == "never trust a blurry hand"
            assert row.scope == "this_checkpoint"
            assert row.weight == 1
            assert row.reinforces_id is None
        await engine.dispose()
    asyncio.run(run())


def test_correction_reinforce_increments_target_weight():
    async def run():
        engine, factory = await _make_factory()
        original_payload = ReviewCorrectionIn(
            run_id=str(uuid.uuid4()), verdict_id=str(uuid.uuid4()), template_id="tpl", checkpoint_id="gen",
            label="false_pass", reason="", lesson="six fingers is a fail", scope="this_pipeline", source="user",
        )
        async with factory() as s:
            original = await review_corrections.upsert_correction(s, USER_A, original_payload)
            assert original.weight == 1

        reinforcing_payload = original_payload.model_copy(update={
            "verdict_id": str(uuid.uuid4()), "reinforces_id": original.id,
        })
        async with factory() as s:
            reinforcing = await review_corrections.upsert_correction(s, USER_A, reinforcing_payload)
            assert reinforcing.reinforces_id == original.id
            assert reinforcing.weight == 1  # the reinforcing row itself starts fresh

        async with factory() as s:
            refreshed = await review_corrections.get_correction_by_id(s, USER_A, original.id)
            assert refreshed.weight == 2
        await engine.dispose()
    asyncio.run(run())


def test_correction_reinforce_foreign_target_is_404_and_not_persisted():
    async def run():
        engine, factory = await _make_factory()
        original_payload = ReviewCorrectionIn(
            run_id=str(uuid.uuid4()), verdict_id=str(uuid.uuid4()), template_id="tpl", checkpoint_id="gen",
            label="false_pass", reason="", lesson="owned by A", scope="this_pipeline", source="user",
        )
        async with factory() as s:
            original = await review_corrections.upsert_correction(s, USER_A, original_payload)

        forged_verdict = str(uuid.uuid4())
        forged_payload = original_payload.model_copy(update={
            "verdict_id": forged_verdict, "reinforces_id": original.id,
        })
        async with factory() as s:
            try:
                await review_corrections.upsert_correction(s, USER_B, forged_payload)
                raise AssertionError("expected ReviewCorrectionError")
            except review_corrections.ReviewCorrectionError as exc:
                assert exc.status_code == 404

        async with factory() as s:
            # Neither the rejected reinforcing row nor a weight bump on A's row landed.
            assert await review_corrections.get_correction(s, USER_B, forged_verdict) is None
            untouched = await review_corrections.get_correction_by_id(s, USER_A, original.id)
            assert untouched.weight == 1
        await engine.dispose()
    asyncio.run(run())


def test_list_corrections_includes_all_pipelines_scope_across_templates():
    async def run():
        engine, factory = await _make_factory()
        scoped_to_tpl = ReviewCorrectionIn(
            run_id=str(uuid.uuid4()), verdict_id=str(uuid.uuid4()), template_id="tpl-a", checkpoint_id="gen",
            label="false_pass", reason="", scope="this_pipeline", source="user",
        )
        global_lesson = ReviewCorrectionIn(
            run_id=str(uuid.uuid4()), verdict_id=str(uuid.uuid4()), template_id="tpl-b", checkpoint_id="gen",
            label="false_fail", reason="", scope="all_pipelines", source="user",
        )
        unrelated = ReviewCorrectionIn(
            run_id=str(uuid.uuid4()), verdict_id=str(uuid.uuid4()), template_id="tpl-c", checkpoint_id="gen",
            label="false_fail", reason="", scope="this_pipeline", source="user",
        )
        async with factory() as s:
            a = await review_corrections.upsert_correction(s, USER_A, scoped_to_tpl)
            g = await review_corrections.upsert_correction(s, USER_A, global_lesson)
            await review_corrections.upsert_correction(s, USER_A, unrelated)
            # Same rows for another user must never leak into USER_A's list.
            await review_corrections.upsert_correction(s, USER_B, global_lesson.model_copy(update={"verdict_id": str(uuid.uuid4())}))

        async with factory() as s:
            rows = await review_corrections.list_corrections(s, USER_A, template_id="tpl-a", limit=10)
            ids = {r.id for r in rows}
            assert ids == {a.id, g.id}
        await engine.dispose()
    asyncio.run(run())


def test_correction_this_model_scope_round_trips_trimmed_model_id():
    """scope="this_model" pins a lesson to one generator model. The model id
    is stored trimmed, survives a re-judge of the same verdict, and is carried
    back out on the read schema so the Go side can filter on it."""

    async def run():
        engine, factory = await _make_factory()
        payload = ReviewCorrectionIn(
            run_id=str(uuid.uuid4()), verdict_id=str(uuid.uuid4()), template_id="tpl-a", checkpoint_id="gen",
            label="false_pass", reason="", lesson="this model flattens faces", scope="this_model",
            model_id="  runware:107@1  ", source="user",
        )
        assert payload.model_id == "runware:107@1"  # trimmed at the schema edge

        async with factory() as s:
            row = await review_corrections.upsert_correction(s, USER_A, payload)
            assert row.scope == "this_model" and row.model_id == "runware:107@1"
            # Re-judging the same verdict must move the model id too, not strand the old one.
            again = await review_corrections.upsert_correction(
                s, USER_A, payload.model_copy(update={"model_id": "google:veo-3"})
            )
            assert again.id == row.id and again.model_id == "google:veo-3"

        async with factory() as s:
            stored = await review_corrections.get_correction(s, USER_A, payload.verdict_id)
            assert ReviewCorrectionOut.model_validate(stored).model_id == "google:veo-3"
        await engine.dispose()

    asyncio.run(run())


@pytest.mark.parametrize("model_id", ["", "   "])
def test_correction_this_model_without_model_id_is_rejected(model_id):
    with pytest.raises(ValidationError):
        ReviewCorrectionIn(run_id="r", verdict_id="v", template_id="t", checkpoint_id="c", label="false_pass",
                           reason="", scope="this_model", model_id=model_id, source="user")


def test_correction_other_scopes_do_not_require_model_id():
    row = ReviewCorrectionIn(run_id="r", verdict_id="v", template_id="t", checkpoint_id="c", label="false_pass",
                             reason="", scope="all_pipelines", source="user")
    assert row.model_id == ""


def test_list_corrections_includes_this_model_scope_across_templates():
    """A this_model lesson minted under one template applies to every pipeline
    that uses that model, so the store returns all of the user's this_model
    rows and lets the Go side filter them by model — but never another
    user's."""

    async def run():
        engine, factory = await _make_factory()
        scoped_to_tpl = ReviewCorrectionIn(
            run_id=str(uuid.uuid4()), verdict_id=str(uuid.uuid4()), template_id="tpl-a", checkpoint_id="gen",
            label="false_pass", reason="", scope="this_pipeline", source="user",
        )
        model_lesson = ReviewCorrectionIn(
            run_id=str(uuid.uuid4()), verdict_id=str(uuid.uuid4()), template_id="tpl-b", checkpoint_id="gen",
            label="false_fail", reason="", scope="this_model", model_id="runware:107@1", source="user",
        )
        other_pipeline = ReviewCorrectionIn(
            run_id=str(uuid.uuid4()), verdict_id=str(uuid.uuid4()), template_id="tpl-c", checkpoint_id="gen",
            label="false_fail", reason="", scope="this_pipeline", source="user",
        )
        async with factory() as s:
            a = await review_corrections.upsert_correction(s, USER_A, scoped_to_tpl)
            m = await review_corrections.upsert_correction(s, USER_A, model_lesson)
            await review_corrections.upsert_correction(s, USER_A, other_pipeline)
            await review_corrections.upsert_correction(s, USER_B, model_lesson.model_copy(update={"verdict_id": str(uuid.uuid4())}))

        async with factory() as s:
            rows = await review_corrections.list_corrections(s, USER_A, template_id="tpl-a", limit=10)
            assert {r.id for r in rows} == {a.id, m.id}
            assert next(r.model_id for r in rows if r.id == m.id) == "runware:107@1"
        await engine.dispose()

    asyncio.run(run())


def test_trace_id_collision_across_users_is_a_conflict_not_a_leak():
    """review_traces' PK is id alone. A collision with a DIFFERENT user's
    trace id must never hand that row back to the colliding caller — it is a
    conflict (409), and the colliding user's own list stays empty."""

    async def run():
        engine, factory = await _make_factory()
        run_id = str(uuid.uuid4())
        trace_id = str(uuid.uuid4())
        payload = ReviewTraceIn(
            id=trace_id, run_id=run_id, checkpoint_id="gen", checkpoint_index=0,
            attempt=1, tier="check", system_prompt="owned by A", raw_output="{}", outcome="pass",
        )
        async with factory() as s:
            owned = await review_traces.create_trace(s, USER_A, payload)
            assert owned.user_id == USER_A

        forged = payload.model_copy(update={"system_prompt": "forged by B"})
        async with factory() as s:
            try:
                await review_traces.create_trace(s, USER_B, forged)
                raise AssertionError("expected ReviewTraceError")
            except review_traces.ReviewTraceError as exc:
                assert exc.status_code == 409

        async with factory() as s:
            # Never leaked into B's own view, and A's row is untouched.
            assert await review_traces.list_traces(s, USER_B, run_id=run_id) == []
            rows = await review_traces.list_traces(s, USER_A, run_id=run_id)
            assert [r.id for r in rows] == [trace_id]
            assert rows[0].system_prompt == "owned by A"
        await engine.dispose()

    asyncio.run(run())


# ── routes ──────────────────────────────────────────────────────────────────
#
# Same harness as the other route tests: a TestClient over the real FastAPI
# app with a MagicMock session, so the wiring (headers, response models,
# error translation) is exercised without a database.

SECRET = "review-traces-test-secret"


async def _session_dep():
    yield MagicMock()


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_SECRET", SECRET)
    app = create_fastapi_app()
    app.dependency_overrides[get_session] = _session_dep
    with TestClient(app) as value:
        yield value


def test_http_routes_require_user_and_secret(client):
    r = client.post("/v1/review-traces", json={}, headers={"X-Internal-Secret": SECRET})
    assert r.status_code in (401, 422)


def test_create_trace_route_maps_id_conflict_to_409(client):
    """The store raises ReviewTraceError(409, ...) on a cross-user id
    collision; the route must translate that to a 409, not a 500 or a
    silently-serialized foreign row."""
    body = {
        "id": str(uuid.uuid4()),
        "run_id": str(uuid.uuid4()),
        "checkpoint_id": "gen",
        "checkpoint_index": 0,
        "tier": "check",
    }
    with patch(
        "app.stores.review_traces.create_trace",
        new=AsyncMock(side_effect=review_traces.ReviewTraceError(409, "trace_id_taken")),
    ):
        response = client.post(
            "/v1/review-traces",
            headers={"X-Internal-Secret": SECRET, "X-User-ID": USER_B},
            json=body,
        )
    assert response.status_code == 409
    assert response.json()["detail"] == "trace_id_taken"


def test_this_model_without_model_id_is_422_over_http(client):
    """The schema rule is the HTTP contract: the Go client gets a 422, not a
    row with an unusable empty model id."""
    body = {
        "run_id": str(uuid.uuid4()),
        "verdict_id": str(uuid.uuid4()),
        "template_id": "tpl-a",
        "checkpoint_id": "gen",
        "label": "false_pass",
        "scope": "this_model",
    }
    response = client.post(
        "/v1/review-corrections",
        headers={"X-Internal-Secret": SECRET, "X-User-ID": USER_A},
        json=body,
    )
    assert response.status_code == 422
