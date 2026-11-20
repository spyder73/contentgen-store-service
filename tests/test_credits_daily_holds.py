"""The daily spend limit counts credits on hold, not only settled debits.

Runs the REAL reserve() SQL (the 24 h window is a bound :since parameter, so it
is portable) on the sqlite ledger fixture of test_credits_hold_release.py.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

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
