"""Per-hold credit release: ledger behaviour against a real SQL engine.

Runs the store's settle/release on in-memory sqlite over the real ORM tables
(the suite's DB approach, see test_review_traces.py) and mirrors the pieces
migration 0010 adds outside the ORM model: the partial unique idempotency
index, the append-only triggers and the per-kind sign CHECKs.

reserve() is Postgres-only SQL (interval arithmetic), so `Ledger.reserve`
applies reserve()'s exact state change: balance -> reserved, plus the hold.

Every test ends with `assert_books`:
  * credits_reserved == the user's open holds (nothing refunded twice,
    nothing left stuck), and
  * balance + reserved == granted + debits (a release only moves money from
    reserved back to balance; only a debit spends it).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import event, select, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

try:  # the HTTP client starlette's TestClient uses (httpx2, else httpx)
    import httpx2 as httpx
except ImportError:  # pragma: no cover - depends on the installed starlette
    import httpx


@compiles(UUID, "sqlite")
def _uuid_sqlite(element, compiler, **kw):  # pragma: no cover - trivial
    return "VARCHAR(36)"


from app.db import get_session  # noqa: E402
from app.fastapi_app import create_fastapi_app  # noqa: E402
from app.models import CreditsLedger, User  # noqa: E402
from app.stores import credits  # noqa: E402
from app.stores.credits import CreditsError  # noqa: E402

USER = str(uuid.uuid4())
OTHER = str(uuid.uuid4())
GRANTED = 1000
SECRET = "test-secret-hold-release"
# Seeded rows get explicit, strictly increasing timestamps well in the past.
T0 = datetime(2020, 1, 1, 12, 0, 0)

_LEDGER_GUARDS = (
    "CREATE UNIQUE INDEX uq_credits_ledger_idempotency ON credits_ledger (idempotency_key)"
    " WHERE idempotency_key IS NOT NULL",
    "CREATE TRIGGER credits_ledger_no_update BEFORE UPDATE ON credits_ledger"
    " BEGIN SELECT RAISE(ABORT, 'credits_ledger is append-only'); END",
    "CREATE TRIGGER credits_ledger_no_delete BEFORE DELETE ON credits_ledger"
    " BEGIN SELECT RAISE(ABORT, 'credits_ledger is append-only'); END",
    "CREATE TRIGGER credits_ledger_signs BEFORE INSERT ON credits_ledger"
    " WHEN (NEW.kind IN ('hold', 'release', 'grant') AND NEW.delta <= 0)"
    "   OR (NEW.kind = 'debit' AND NEW.delta >= 0)"
    " BEGIN SELECT RAISE(ABORT, 'credits_ledger sign check'); END",
)


class Ledger:
    def __init__(self, factory):
        self.factory = factory
        self._tick = 0

    def _at(self) -> datetime:
        self._tick += 1
        return T0 + timedelta(seconds=self._tick)

    async def _move(self, s, user: str, to_reserved: int) -> None:
        await s.execute(
            text(
                "UPDATE users SET credits_balance = credits_balance - :a,"
                " credits_reserved = credits_reserved + :a WHERE id = :u"
            ),
            {"a": to_reserved, "u": user},
        )

    async def reserve(self, run: str, cp: str | None, amount: int, attempt: int = 1, user: str = USER):
        async with self.factory() as s:
            await self._move(s, user, amount)
            s.add(CreditsLedger(
                user_id=user, kind="hold", delta=amount, pipeline_run_id=run,
                checkpoint_id=cp, attempt=attempt, created_at=self._at(),
                idempotency_key=f"reserve|{user}|{run}|{cp}|{attempt}",
            ))
            await s.commit()

    async def legacy_release(self, run: str, amount: int, reason: str, user: str = USER):
        """What release() wrote before this fix: ONE aggregate row, no checkpoint."""
        async with self.factory() as s:
            await self._move(s, user, -amount)
            s.add(CreditsLedger(
                user_id=user, kind="release", delta=amount, pipeline_run_id=run,
                note=reason, created_at=self._at(), idempotency_key=f"legacy|{run}|{reason}",
            ))
            await s.commit()

    async def release(self, run: str, reason: str, *, key: str | None = None,
                      cp: str | None = None, attempt: int | None = None, user: str = USER) -> dict:
        # Run-wide calls pass no hold key at all, exactly like an old backend.
        hold = {} if cp is None and attempt is None else {"checkpoint_id": cp, "attempt": attempt}
        async with self.factory() as s:
            return await credits.release(
                s, user_id=user, pipeline_run_id=run, reason=reason,
                idempotency_key=key or f"release|{run}|{reason}", **hold,
            )

    async def settle(self, run: str, cp: str, usd: str, *, attempt: int = 1,
                     key: str | None = None, user: str = USER) -> dict:
        async with self.factory() as s:
            return await credits.settle(
                s, user_id=user, pipeline_run_id=run, checkpoint_id=cp, attempt=attempt,
                actual_cost_usd=usd, provider="runware", model="flux-dev",
                cost_source="provider_telemetry",
                idempotency_key=key or f"settle|{run}|{cp}|{attempt}",
            )

    async def money(self, user: str = USER) -> tuple[int, int]:
        async with self.factory() as s:
            bv = await credits.get_balance(s, user)
            return bv.balance, bv.reserved

    async def rows(self, run: str, kind: str) -> list[CreditsLedger]:
        async with self.factory() as s:
            res = await s.execute(
                select(CreditsLedger)
                .where(CreditsLedger.pipeline_run_id == run, CreditsLedger.kind == kind)
            )
            return list(res.scalars().all())

    async def open_holds(self, run: str, user: str = USER) -> list[tuple]:
        async with self.factory() as s:
            return await credits._open_holds(s, user, run)

    async def assert_books(self, user: str = USER) -> None:
        async with self.factory() as s:
            bv = await credits.get_balance(s, user)
            res = await s.execute(
                select(CreditsLedger.kind, CreditsLedger.delta, CreditsLedger.pipeline_run_id)
                .where(CreditsLedger.user_id == user)
            )
            rows = res.all()
            runs = {r[2] for r in rows if r[0] == "hold"}
            open_total = 0
            for run in runs:
                open_total += sum(a for _, _, a in await credits._open_holds(s, user, run))
            debits = sum(r[1] for r in rows if r[0] == "debit")
        assert bv.reserved == open_total, (bv.reserved, open_total)
        assert bv.balance + bv.reserved == GRANTED + debits, (bv.balance, bv.reserved, debits)


@pytest_asyncio.fixture()
async def ledger():
    engine = create_async_engine("sqlite+aiosqlite://")
    # Keep uuids as the plain strings the store passes around. Otherwise
    # SQLAlchemy stores UUID columns as dashless hex on sqlite while the
    # store's raw-SQL params reach the DB untouched, and nothing would match
    # (Postgres compares them as uuid values).
    engine.sync_engine.dialect.supports_native_uuid = True

    @event.listens_for(engine.sync_engine, "connect")
    def _register(dbapi_conn, _record):
        dbapi_conn.create_function("gen_random_uuid", 0, lambda: str(uuid.uuid4()))

    async with engine.begin() as conn:
        for table in (User.__table__, CreditsLedger.__table__):
            await conn.run_sync(lambda c, t=table: t.create(c))
        for ddl in _LEDGER_GUARDS:
            await conn.exec_driver_sql(ddl)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        s.add(User(id=USER, username="u", password_hash="x", credits_balance=GRANTED))
        s.add(User(id=OTHER, username="o", password_hash="x", credits_balance=GRANTED))
        await s.commit()
    yield Ledger(factory)
    await engine.dispose()


def _run() -> str:
    return str(uuid.uuid4())


# ── S1: per-hold release ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_per_hold_release_closes_exactly_its_hold(ledger):
    run = _run()
    await ledger.reserve(run, "gen-a", 100)
    await ledger.reserve(run, "gen-b", 50)
    await ledger.reserve(_run(), "gen-a", 70)  # same checkpoint, other run
    await ledger.reserve(run, "gen-a", 40, user=OTHER)  # same hold key, other user
    assert await ledger.money() == (780, 220)

    out = await ledger.release(run, "gen_a_not_settled", key="rel-a", cp="gen-a", attempt=1)
    assert out == {"status": "released", "balance": 880, "reserved": 120, "returned": 100}
    [row] = await ledger.rows(run, "release")
    assert (row.checkpoint_id, row.attempt, row.delta, row.note, row.idempotency_key) == (
        "gen-a", 1, 100, "gen_a_not_settled", "rel-a",
    )

    # Closed now: another per-hold release of it (new key) returns nothing,
    # and neither does a release of an attempt that was never reserved.
    again = await ledger.release(run, "other_reason", key="rel-a-2", cp="gen-a", attempt=1)
    assert again["status"] == "nothing_to_release"
    wrong_attempt = await ledger.release(run, "x", key="rel-a-3", cp="gen-a", attempt=2)
    assert wrong_attempt["status"] == "nothing_to_release"
    assert await ledger.money() == (880, 120)
    assert await ledger.open_holds(run) == [("gen-b", 1, 50)]
    assert await ledger.money(OTHER) == (960, 40)
    await ledger.assert_books()
    await ledger.assert_books(OTHER)


@pytest.mark.asyncio
async def test_per_hold_release_replay_answers_already_released(ledger):
    run = _run()
    await ledger.reserve(run, "gen-a", 100)
    await ledger.release(run, "r", key="rel-a", cp="gen-a", attempt=1)
    await ledger.reserve(run, "gen-b", 50)
    replay = await ledger.release(run, "r", key="rel-a", cp="gen-a", attempt=1)
    assert replay == {"status": "already_released", "balance": 950, "reserved": 50}
    assert len(await ledger.rows(run, "release")) == 1
    await ledger.assert_books()


# ── S2: run-wide release ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_wide_release_after_per_hold_release_returns_only_the_rest(ledger):
    run = _run()
    await ledger.reserve(run, "gen-a", 100)
    await ledger.reserve(run, "gen-b", 50)
    await ledger.reserve(run, "gen-c", 30, attempt=2)
    await ledger.release(run, "gen_a_not_settled", key="rel-a", cp="gen-a", attempt=1)

    out = await ledger.release(run, "pipeline_completed", key="done")
    assert out == {"status": "released", "balance": 1000, "reserved": 0, "returned": 80}
    got = sorted(
        (r.checkpoint_id, r.attempt, r.delta, r.note, r.idempotency_key)
        for r in await ledger.rows(run, "release")
    )
    assert got == [
        ("gen-a", 1, 100, "gen_a_not_settled", "rel-a"),
        ("gen-b", 1, 50, "pipeline_completed", "done"),  # oldest open hold: caller's key
        ("gen-c", 2, 30, "pipeline_completed", "done#gen-c#2"),
    ]
    await ledger.assert_books()


@pytest.mark.asyncio
async def test_two_run_wide_releases_with_different_reasons_refund_each_hold_once(ledger):
    run = _run()
    await ledger.reserve(run, "gen-a", 100)
    await ledger.reserve(run, "gen-b", 50)

    first = await ledger.release(run, "checkpoint_failed")
    assert first["returned"] == 150
    second = await ledger.release(run, "pipeline_completed")
    assert second["status"] == "nothing_to_release"
    assert await ledger.money() == (1000, 0)

    # A hold placed after the first release (e.g. a retry) is returned once,
    # by the next run-wide release, and only it.
    await ledger.reserve(run, "gen-a", 40, attempt=2)
    third = await ledger.release(run, "pipeline_cancelled")
    assert third["returned"] == 40
    assert (await ledger.release(run, "pipeline_failed"))["status"] == "nothing_to_release"
    assert await ledger.money() == (1000, 0)
    await ledger.assert_books()


@pytest.mark.asyncio
async def test_run_wide_release_replay_answers_already_released(ledger):
    run = _run()
    await ledger.reserve(run, "gen-a", 100)
    await ledger.reserve(run, "gen-b", 50)
    await ledger.release(run, "checkpoint_failed", key="k")
    await ledger.reserve(run, "gen-c", 25)
    replay = await ledger.release(run, "checkpoint_failed", key="k")
    assert replay == {"status": "already_released", "balance": 975, "reserved": 25}
    assert len(await ledger.rows(run, "release")) == 2
    await ledger.assert_books()


@pytest.mark.asyncio
async def test_hold_without_checkpoint_is_released_once(ledger):
    """No caller reserves without a checkpoint today; if one did, its hold must
    still come back exactly once."""
    run = _run()
    await ledger.reserve(run, None, 60)
    assert (await ledger.release(run, "a"))["returned"] == 60
    assert (await ledger.release(run, "b"))["status"] == "nothing_to_release"
    await ledger.assert_books()


# ── S3: legacy aggregate release rows ────────────────────────────────────────


@pytest.mark.asyncio
async def test_legacy_null_checkpoint_release_closes_earlier_holds(ledger):
    run = _run()
    await ledger.reserve(run, "gen-a", 100)
    await ledger.reserve(run, "gen-b", 50)
    await ledger.legacy_release(run, 150, "checkpoint_failed")
    await ledger.reserve(run, "gen-c", 40)  # placed after the legacy release
    assert await ledger.money() == (960, 40)

    out = await ledger.release(run, "pipeline_completed")
    assert out["returned"] == 40  # not 190: gen-a/gen-b came back already
    assert (await ledger.release(run, "gen_a_not_settled", key="a", cp="gen-a", attempt=1))[
        "status"
    ] == "nothing_to_release"
    assert await ledger.money() == (1000, 0)
    await ledger.assert_books()


# ── S4: settle of a hold a release already closed ────────────────────────────


@pytest.mark.asyncio
async def test_settle_after_per_hold_release_debits_actual_only(ledger):
    run = _run()
    await ledger.reserve(run, "gen-a", 100)
    await ledger.release(run, "gen_a_not_settled", key="rel-a", cp="gen-a", attempt=1)
    assert await ledger.money() == (1000, 0)

    out = await ledger.settle(run, "gen-a", "0.10")  # 17 credits
    assert out == {"status": "settled", "balance": 983, "reserved": 0}
    [debit] = await ledger.rows(run, "debit")
    assert debit.delta == -17
    # No settle_slack refund: the only release row is the per-hold release.
    assert [r.note for r in await ledger.rows(run, "release")] == ["gen_a_not_settled"]
    assert (await ledger.release(run, "pipeline_completed"))["status"] == "nothing_to_release"
    await ledger.assert_books()


@pytest.mark.asyncio
async def test_settle_of_call_caught_in_flight_by_run_wide_release(ledger):
    """Old backend: a failed call's defer releases the whole run, including a
    sibling call still in flight; that sibling then settles."""
    run = _run()
    await ledger.reserve(run, "gen-a", 100)
    await ledger.reserve(run, "gen-b", 60)
    await ledger.release(run, "gen_a_not_settled")  # returns both holds
    assert await ledger.money() == (1000, 0)

    out = await ledger.settle(run, "gen-b", "0.20")  # 34 credits
    assert out == {"status": "settled", "balance": 966, "reserved": 0}
    await ledger.assert_books()


@pytest.mark.asyncio
async def test_settle_after_legacy_release_debits_actual_only(ledger):
    run = _run()
    await ledger.reserve(run, "gen-a", 100)
    await ledger.legacy_release(run, 100, "checkpoint_failed")
    out = await ledger.settle(run, "gen-a", "0.10")
    assert out == {"status": "settled", "balance": 983, "reserved": 0}
    await ledger.assert_books()


@pytest.mark.asyncio
async def test_settle_after_release_short_balance_records_shortfall(ledger):
    run = _run()
    await ledger.reserve(run, "gen-a", 100)
    await ledger.reserve(run, "gen-d", 20)
    await ledger.release(run, "r")  # both back: 1000 / 0
    await ledger.reserve(_run(), "big", 990)  # balance spent elsewhere: 10 / 990

    with pytest.raises(CreditsError) as exc:
        await ledger.settle(run, "gen-a", "1.00")  # 170 credits, 10 covered
    assert exc.value.code == "balance_exhausted"
    assert exc.value.extra == {"debited": 10, "uncovered": 160}
    assert await ledger.money() == (0, 990)  # reserved untouched

    # Empty balance: nothing to debit and a zero debit is illegal, so the
    # shortfall row carries the settle's idempotency key.
    with pytest.raises(CreditsError) as exc:
        await ledger.settle(run, "gen-d", "0.10", key="settle-d")  # 17 credits
    assert exc.value.extra == {"debited": 0, "uncovered": 17}
    debits = {r.checkpoint_id: r.delta for r in await ledger.rows(run, "debit")}
    assert debits == {"gen-a": -10}
    adjusts = {r.checkpoint_id: (r.delta, r.idempotency_key) for r in await ledger.rows(run, "adjust")}
    assert adjusts == {"gen-a": (-160, None), "gen-d": (-17, "settle-d")}
    replay = await ledger.settle(run, "gen-d", "0.10", key="settle-d")
    assert replay == {"status": "already_settled", "balance": 0, "reserved": 990}
    await ledger.assert_books()


@pytest.mark.asyncio
async def test_settle_of_open_hold_keeps_slack_refund(ledger):
    run = _run()
    await ledger.reserve(run, "gen-a", 100)
    out = await ledger.settle(run, "gen-a", "0.10")  # 17 of 100 held
    assert out == {"status": "settled", "balance": 983, "reserved": 0}
    [slack] = await ledger.rows(run, "release")
    assert (slack.note, slack.delta) == ("settle_slack", 83)
    assert (await ledger.release(run, "pipeline_completed"))["status"] == "nothing_to_release"
    await ledger.assert_books()


# ── route: old and new backend bodies ────────────────────────────────────────


@pytest.mark.asyncio
async def test_release_route_old_and_new_bodies(ledger, monkeypatch):
    monkeypatch.setenv("INTERNAL_API_SECRET", SECRET)
    app = create_fastapi_app()

    async def _session():
        async with ledger.factory() as s:
            yield s

    app.dependency_overrides[get_session] = _session
    run = _run()
    await ledger.reserve(run, "gen-a", 100)
    await ledger.reserve(run, "gen-b", 50)
    await ledger.reserve(run, "gen-c", 30)
    headers = {"X-User-ID": USER, "X-Internal-Secret": SECRET}
    url = f"/v1/users/{USER}/credits/release"
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://store") as c:
        new = await c.post(url, headers=headers, json={
            "pipeline_run_id": run, "reason": "gen_a_not_settled", "idempotency_key": "k-a",
            "checkpoint_id": "gen-a", "attempt": 1,
        })
        assert new.status_code == 200
        assert new.json() == {"status": "released", "balance": 920, "reserved": 80, "returned": 100}

        # An old backend sends no checkpoint fields: run-wide, the rest only.
        old = await c.post(url, headers=headers, json={
            "pipeline_run_id": run, "reason": "pipeline_completed", "idempotency_key": "k-done",
        })
        assert old.status_code == 200
        assert old.json() == {"status": "released", "balance": 1000, "reserved": 0, "returned": 80}

        replay = await c.post(url, headers=headers, json={
            "pipeline_run_id": run, "reason": "pipeline_completed", "idempotency_key": "k-done",
        })
        assert replay.json()["status"] == "already_released"
    await ledger.assert_books()
