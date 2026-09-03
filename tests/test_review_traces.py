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
from unittest.mock import MagicMock


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(element, compiler, **kw):  # pragma: no cover - trivial
    return "JSON"


@compiles(UUID, "sqlite")
def _uuid_sqlite(element, compiler, **kw):  # pragma: no cover - trivial
    return "VARCHAR(36)"


from app.db import get_session  # noqa: E402
from app.fastapi_app import create_fastapi_app  # noqa: E402
from app.models import ReviewCorrection, ReviewTrace, User  # noqa: E402
from app.schemas import ReviewCorrectionIn, ReviewTraceIn  # noqa: E402
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
