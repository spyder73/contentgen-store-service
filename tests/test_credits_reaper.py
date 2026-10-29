from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles


@compiles(UUID, "sqlite")
def _uuid_sqlite(element, compiler, **kw):  # pragma: no cover - trivial
    return "VARCHAR(36)"


from app.db import get_session  # noqa: E402
from app.fastapi_app import create_fastapi_app  # noqa: E402
from app.models import CreditsLedger, User  # noqa: E402
from app.stores import credits, credits_reaper  # noqa: E402

INTERNAL_SECRET = "test-secret-reaper"

U = "00000000-0000-0000-0000-000000000001"
NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
RULES = [{"checkpoint": "lora_training", "match": "prefix", "older_than_s": 86400},
         {"checkpoint": "builder_clipstyle_understand", "match": "exact", "older_than_s": 3600}]


def _hold(run, checkpoint, hours, amount=5, kind="hold"):
    return CreditsLedger(id=str(uuid.uuid4()), user_id=U, kind=kind, delta=amount, pipeline_run_id=run, checkpoint_id=checkpoint,
                         attempt=1, idempotency_key=f"{kind}-{run}-{checkpoint}", created_at=NOW - timedelta(hours=hours))


def _run(rows, fn):
    async def go():
        engine = create_async_engine("sqlite+aiosqlite://")
        engine.sync_engine.dialect.supports_native_uuid = True

        @event.listens_for(engine.sync_engine, "connect")
        def _register(dbapi_conn, _record):
            dbapi_conn.create_function("gen_random_uuid", 0, lambda: str(uuid.uuid4()))

        async with engine.begin() as conn:
            for table in (User.__table__, CreditsLedger.__table__):
                await conn.run_sync(lambda c, t=table: t.create(c))
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as s:
            s.add(User(id=U, username="u", password_hash="x", credits_balance=100, credits_reserved=20))
            s.add_all(rows)
            await s.commit()
        try:
            async with factory() as s:
                return await fn(s)
        finally:
            await engine.dispose()

    return asyncio.run(go())


RUN_A, RUN_B, RUN_C, RUN_D = (str(uuid.uuid4()) for _ in range(4))

# The operator's handler holds: the backend files one under (request id, handler name), a request
# id per call, so a hold cut off by a restart is found by its handler name alone.
OPERATOR_RULES = [{"checkpoint": "operator_chat", "match": "exact", "older_than_s": 3600},
                  {"checkpoint": "operator_sourcing", "match": "exact", "older_than_s": 3600}]


def test_reaper_releases_only_old_open_holds_of_the_rules():
    rows = [
        _hold(RUN_A, "lora_training:job-1", hours=30),         # stale training hold: reaped
        _hold(RUN_B, "lora_training:job-2", hours=2),          # a training still running: kept
        _hold(RUN_C, "builder_clipstyle_understand", hours=3),  # stale classify hold: reaped
        _hold(RUN_D, "clip_render", hours=50),                  # not a listed kind: kept
        _hold(RUN_D, "lora_training:job-3", hours=40),
        _hold(RUN_D, "lora_training:job-3", hours=39, kind="debit"),  # already settled: kept
    ]

    async def fn(s):
        reaped = await credits_reaper.reap_stale_holds(s, RULES, now=NOW, limit=100)
        user = await s.get(User, U)
        return sorted(h["checkpoint_id"] for h in reaped), user.credits_balance, user.credits_reserved

    checkpoints, balance, reserved = _run(rows, fn)
    assert checkpoints == ["builder_clipstyle_understand", "lora_training:job-1"]
    assert (balance, reserved) == (110, 10)


def test_a_stale_operator_hold_is_reaped_and_a_fresh_one_is_not():
    rows = [
        _hold(RUN_A, "operator_sourcing", hours=2),                  # a paid search cut off by a restart: reaped
        _hold(RUN_B, "operator_sourcing", hours=0.2),                # a search still in flight: kept
        _hold(RUN_C, "operator_chat", hours=2),                      # a chat turn cut off: reaped
        _hold(RUN_D, "operator_sourcing", hours=3),
        _hold(RUN_D, "operator_sourcing", hours=2.5, kind="debit"),  # settled: kept
    ]

    async def fn(s):
        reaped = await credits_reaper.reap_stale_holds(s, OPERATOR_RULES, now=NOW, limit=100)
        user = await s.get(User, U)
        return sorted((h["pipeline_run_id"], h["checkpoint_id"]) for h in reaped), user.credits_balance, user.credits_reserved

    reaped, balance, reserved = _run(rows, fn)
    assert reaped == sorted([(RUN_A, "operator_sourcing"), (RUN_C, "operator_chat")])
    assert (balance, reserved) == (110, 10)


def test_reaping_twice_releases_once():
    async def fn(s):
        first = await credits_reaper.reap_stale_holds(s, RULES, now=NOW, limit=100)
        second = await credits_reaper.reap_stale_holds(s, RULES, now=NOW, limit=100)
        releases = (await s.execute(select(CreditsLedger).where(CreditsLedger.kind == "release"))).scalars().all()
        return len(first), len(second), [r.note for r in releases]

    assert _run([_hold(RUN_A, "lora_training:job-1", hours=30)], fn) == (1, 0, ["stale_hold_reaped"])


def test_an_underscore_in_a_prefix_is_not_a_wildcard():
    async def fn(s):
        return [h["checkpoint_id"] for h in await credits_reaper.reap_stale_holds(s, RULES, now=NOW, limit=100)]

    assert _run([_hold(RUN_A, "loraXtraining:job-9", hours=30)], fn) == []


def test_one_release_failing_does_not_abort_the_pass(monkeypatch):
    """PF4-15: wrap each release in try/except so one bad hold never aborts the pass."""
    real_release = credits.release

    async def flaky_release(session, **kwargs):
        if kwargs.get("pipeline_run_id") == RUN_A:
            raise RuntimeError("boom")
        return await real_release(session, **kwargs)

    monkeypatch.setattr(credits, "release", flaky_release)

    rows = [
        _hold(RUN_A, "lora_training:job-1", hours=30),  # release raises: logged and skipped
        _hold(RUN_B, "lora_training:job-2", hours=40),   # still released despite the above
    ]

    async def fn(s):
        reaped = await credits_reaper.reap_stale_holds(s, RULES, now=NOW, limit=100)
        user = await s.get(User, U)
        return [h["checkpoint_id"] for h in reaped], user.credits_balance, user.credits_reserved

    checkpoints, balance, reserved = _run(rows, fn)
    assert checkpoints == ["lora_training:job-2"]
    assert (balance, reserved) == (105, 15)


def test_the_credits_reported_are_what_release_actually_returned_not_the_stale_sum():
    """X-M10 M2: `_STALE_SQL`'s own SUM filters by age BEFORE grouping, so a fresher hold row in the
    same (checkpoint, attempt) group is excluded from it — but `credits.release`'s own `_open_holds`
    sums every open hold of that group with no age filter, and returns the lot. The reaper must
    report release()'s true `returned` amount, not its own narrower SUM."""
    rows = [
        _hold(RUN_A, "lora_training:job-1", hours=30, amount=5),  # older than the cutoff: counted
        CreditsLedger(
            id=str(uuid.uuid4()), user_id=U, kind="hold", delta=7, pipeline_run_id=RUN_A,
            checkpoint_id="lora_training:job-1", attempt=1, idempotency_key=f"hold-{RUN_A}-fresh",
            created_at=NOW - timedelta(hours=1),  # fresher than the cutoff: excluded from the stale SUM,
        ),                                        # but still returned by release() alongside the other.
    ]

    async def fn(s):
        reaped = await credits_reaper.reap_stale_holds(s, RULES, now=NOW, limit=100)
        return reaped[0]["credits"]

    assert _run(rows, fn) == 12


def test_a_non_released_outcome_rolls_back_so_the_user_row_lock_is_freed(monkeypatch):
    """X-M10 M4: release()'s already_released/nothing_to_release paths write nothing, but the
    FOR NO KEY UPDATE user-row lock taken by `_lock_user_row` is still held until the transaction
    ends. The reaper must roll back after those outcomes too (not only after an exception), so the
    lock is freed for the rest of the pass instead of outliving it."""
    monkeypatch.setattr(credits, "release", AsyncMock(return_value={"status": "nothing_to_release", "balance": 0, "reserved": 0}))

    async def fn(s):
        calls = []
        real_rollback = s.rollback

        async def spy_rollback():
            calls.append(1)
            await real_rollback()

        s.rollback = spy_rollback
        await credits_reaper.reap_stale_holds(s, RULES, now=NOW, limit=100)
        return calls

    assert _run([_hold(RUN_A, "lora_training:job-1", hours=30)], fn) == [1]


# ── X-M8: the reaper endpoint's checkpoint allowlist ─────────────────────────


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_SECRET", INTERNAL_SECRET)


@pytest.fixture()
def client():
    from unittest.mock import MagicMock

    async def _mock_session():
        yield MagicMock()

    app = create_fastapi_app()
    app.dependency_overrides[get_session] = _mock_session
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


def test_the_reaper_endpoint_refuses_a_checkpoint_kind_outside_the_allowlist(client):
    resp = client.post(
        "/v1/internal/credits/reap-stale-holds",
        json={"rules": [{"checkpoint": "clip_render", "match": "prefix", "older_than_s": 3600}]},
        headers={"X-Internal-Secret": INTERNAL_SECRET},
    )
    assert resp.status_code == 422


def test_the_reaper_endpoint_refuses_an_allowlisted_checkpoint_with_the_wrong_match(client):
    resp = client.post(
        "/v1/internal/credits/reap-stale-holds",
        json={"rules": [{"checkpoint": "lora_training", "match": "exact", "older_than_s": 3600}]},
        headers={"X-Internal-Secret": INTERNAL_SECRET},
    )
    assert resp.status_code == 422


def test_the_reaper_endpoint_accepts_the_allowlisted_rules(client, monkeypatch):
    # RULES plus the operator's two handler kinds: the backend sends all four in one request, and
    # one rule outside the allowlist refuses the whole pass.
    monkeypatch.setattr(credits_reaper, "reap_stale_holds", AsyncMock(return_value=[]))
    resp = client.post(
        "/v1/internal/credits/reap-stale-holds",
        json={"rules": RULES + OPERATOR_RULES, "limit": 50},
        headers={"X-Internal-Secret": INTERNAL_SECRET},
    )
    assert resp.status_code == 200
    assert resp.json() == {"released": 0, "holds": []}
