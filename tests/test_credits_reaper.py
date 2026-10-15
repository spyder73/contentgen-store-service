from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import event, select
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles


@compiles(UUID, "sqlite")
def _uuid_sqlite(element, compiler, **kw):  # pragma: no cover - trivial
    return "VARCHAR(36)"


from app.models import CreditsLedger, User  # noqa: E402
from app.stores import credits, credits_reaper  # noqa: E402

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
