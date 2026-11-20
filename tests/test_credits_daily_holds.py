"""The daily spend limit counts credits on hold, not only settled debits.

Runs the REAL reserve() SQL (the 24 h window is a bound :since parameter, so it
is portable) on the sqlite ledger fixture of test_credits_hold_release.py.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.models import CreditsLedger

from app.stores import credits
from app.stores.credits import CreditsError

from .test_credits_hold_release import USER, _run, ledger  # noqa: F401  (fixture)

LIMIT = 1000


async def _reserve(ledger, run: str, cp: str, amount: int = 400) -> dict:
    async with ledger.factory() as s:
        return await credits.reserve(
            s, user_id=USER, amount=amount, pipeline_run_id=run, checkpoint_id=cp,
            attempt=1, idempotency_key=f"reserve|{USER}|{run}|{cp}|1",
        )


async def _set_limit(ledger) -> None:
    async with ledger.factory() as s:
        await s.execute(
            text("UPDATE users SET credits_balance = 5000, daily_spend_limit = :l WHERE id = :u"),
            {"l": LIMIT, "u": USER},
        )
        await s.commit()


@pytest.mark.asyncio
async def test_open_holds_count_toward_the_daily_limit(ledger):
    await _set_limit(ledger)
    await _reserve(ledger, _run(), "a")
    await _reserve(ledger, _run(), "b")
    with pytest.raises(CreditsError) as e:
        await _reserve(ledger, _run(), "c")
    assert e.value.code == "daily_limit_exceeded"
    assert e.value.have == 200
    assert e.value.need == 400
    assert e.value.extra == {"daily_limit": LIMIT, "spent_today": 800}


@pytest.mark.asyncio
async def test_a_released_hold_frees_the_daily_room(ledger):
    await _set_limit(ledger)
    run = _run()
    await _reserve(ledger, run, "a")
    await _reserve(ledger, _run(), "b")
    await ledger.release(run, "not_needed", cp="a", attempt=1)
    out = await _reserve(ledger, _run(), "c")
    assert out["status"] == "reserved"


@pytest.mark.asyncio
async def test_settled_spend_still_counts(ledger):
    await _set_limit(ledger)
    run = _run()
    await _reserve(ledger, run, "a")
    await _reserve(ledger, _run(), "b")
    # $2.35 -> 400 credits after the 1.7x markup: the hold settles into a debit.
    await ledger.settle(run, "a", "2.35")
    with pytest.raises(CreditsError) as e:
        await _reserve(ledger, _run(), "c")
    assert e.value.have == 200
    assert e.value.extra["spent_today"] == 800


async def _old_open_hold(ledger, cp: str, amount: int, hours: int = 30) -> None:
    """An orphan: a hold row created `hours` ago and never closed (the ledger is
    append-only, so it is inserted aged, and credits_reserved carries it)."""
    async with ledger.factory() as s:
        await s.execute(
            text("UPDATE users SET credits_balance = credits_balance - :a,"
                 " credits_reserved = credits_reserved + :a WHERE id = :u"),
            {"a": amount, "u": USER},
        )
        s.add(CreditsLedger(
            user_id=USER, kind="hold", delta=amount, pipeline_run_id=_run(), checkpoint_id=cp,
            attempt=1, idempotency_key=f"orphan|{cp}",
            created_at=datetime.now(timezone.utc) - timedelta(hours=hours),
        ))
        await s.commit()


@pytest.mark.asyncio
async def test_an_orphaned_hold_older_than_a_day_does_not_use_up_todays_room(ledger):
    await _set_limit(ledger)
    await _old_open_hold(ledger, "orphan", 900)
    out = await _reserve(ledger, _run(), "today", amount=900)
    assert out["status"] == "reserved"


@pytest.mark.asyncio
async def test_a_hold_opened_today_still_uses_up_room(ledger):
    await _set_limit(ledger)
    await _reserve(ledger, _run(), "a", amount=900)
    with pytest.raises(CreditsError) as e:
        await _reserve(ledger, _run(), "b", amount=400)
    assert e.value.extra["spent_today"] == 900


@pytest.mark.asyncio
async def test_second_reserve_sees_the_first_hold_row_and_takes_the_user_lock(ledger, monkeypatch):
    """sqlite has no row locks, so the race itself cannot be expressed. Tested
    instead: the lock call runs before the reserve UPDATE, and a reserve that
    runs after the first one's hold row exists cannot pass a limit the two
    would jointly exceed (counts the hold ROW, not only credits_reserved)."""
    await _set_limit(ledger)
    calls: list[str] = []
    real = credits._lock_user_row

    async def spy(session, user_id):
        calls.append("lock")
        await real(session, user_id)

    monkeypatch.setattr(credits, "_lock_user_row", spy)
    await _reserve(ledger, _run(), "a", amount=600)
    # Simulate drift: credits_reserved lags the hold row (the old predicate
    # would let the second through; the ledger count must not).
    async with ledger.factory() as s:
        await s.execute(text("UPDATE users SET credits_reserved = 0 WHERE id = :u"), {"u": USER})
        await s.commit()
    with pytest.raises(CreditsError) as e:
        await _reserve(ledger, _run(), "b", amount=600)
    assert e.value.code == "daily_limit_exceeded"
    assert calls == ["lock", "lock"]
