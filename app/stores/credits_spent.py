"""What a user actually paid, summed per caller-defined scope.

A "scope" is a set of ledger keys (`credits_ledger.pipeline_run_id`) the
caller groups under one name — the backend asks for one per clip: the clip's
own id (the key its clip-side generations reserve under) plus the pipeline
runs that produced it. Read-only: it never touches balance or holds.

Only `debit` rows count as spent. A debit is what settle took from the user —
credits with the markup already applied, and the raw provider USD beside it —
so the sum is the bill, not a re-pricing. Holds still open are not spent yet,
releases were refunded, and an `adjust` shortfall is a debt the balance did
not cover, not money paid.

What is still held is reported beside it (`held_usd`): the holds of the scope
with no settle or release yet, so a caller that budgets a scope can count the
calls in flight as well as the ones that settled.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models import CreditsLedger
from app.stores.credits import credits_to_usd


_USD_PLACES = Decimal("0.000001")  # the ledger's NUMERIC(12, 6)


def _ledger_key(raw: str) -> str | None:
    """The canonical uuid string, or None for anything the UUID column could
    not hold (it would fail the whole query on Postgres)."""
    try:
        return str(uuid.UUID(str(raw).strip()))
    except (ValueError, AttributeError, TypeError):
        return None


async def _held_credits_by_key(session: AsyncSession, user_id: str, keys: set[str]) -> dict[str, int]:
    """Credits of the user's open holds per ledger key, in one query.

    A hold is open when nothing closed it, by the same two rules as
    `credits._OPEN_HOLDS_SQL` (the source of truth, which settle and release act on):
    a `debit` or `release` row with the same (run, checkpoint, attempt), or a legacy
    run-wide `release` row (no checkpoint) written at or after the hold.
    """
    hold, closer, run_wide = (aliased(CreditsLedger) for _ in range(3))
    closed_by_key = exists().where(
        closer.user_id == hold.user_id,
        closer.pipeline_run_id == hold.pipeline_run_id,
        closer.kind.in_(("debit", "release")),
        closer.checkpoint_id == hold.checkpoint_id,
        closer.attempt == hold.attempt,
    )
    closed_run_wide = exists().where(
        run_wide.user_id == hold.user_id,
        run_wide.pipeline_run_id == hold.pipeline_run_id,
        run_wide.kind == "release",
        run_wide.checkpoint_id.is_(None),
        run_wide.created_at >= hold.created_at,
    )
    rows = await session.execute(
        select(hold.pipeline_run_id, func.coalesce(func.sum(hold.delta), 0))
        .where(
            hold.user_id == user_id,
            hold.kind == "hold",
            hold.pipeline_run_id.in_(sorted(keys)),
            ~closed_by_key,
            ~closed_run_wide,
        )
        .group_by(hold.pipeline_run_id)
    )
    return {str(key): int(credits) for key, credits in rows.all()}


async def _debited_by_key(
    session: AsyncSession, user_id: str, keys: set[str]
) -> dict[str, tuple[int, Decimal, int]]:
    """(credits, raw USD, debit count) of the user's debits per ledger key, in one query."""
    rows = await session.execute(
        select(
            CreditsLedger.pipeline_run_id,
            func.coalesce(func.sum(-CreditsLedger.delta), 0),
            func.coalesce(func.sum(CreditsLedger.cost_usd), 0),
            func.count(),
        )
        .where(
            CreditsLedger.user_id == user_id,
            CreditsLedger.kind == "debit",
            CreditsLedger.pipeline_run_id.in_(sorted(keys)),
        )
        .group_by(CreditsLedger.pipeline_run_id)
    )
    return {str(key): (int(credits), Decimal(str(usd)), int(count)) for key, credits, usd, count in rows.all()}


async def spent_by_scope(
    session: AsyncSession, user_id: str, scopes: dict[str, list[str]]
) -> dict[str, dict]:
    """{scope: {credits, usd, held_usd, debit_count}} over the user's ledger rows
    keyed by any of the scope's ledger keys. `usd` is the raw USD of the scope's
    debits; `held_usd` is what its open holds would cost (their credits converted
    back, so a hold is shown at most one credit above the estimate it was rounded
    up from). A key shared by two scopes counts in both (each scope is answered
    on its own); a scope with no rows answers zeros. Another user's rows never
    match: user_id is part of the filter."""
    keys_by_scope = {
        scope: {k for k in (_ledger_key(raw) for raw in raws) if k}
        for scope, raws in scopes.items()
    }
    all_keys = set().union(*keys_by_scope.values()) if keys_by_scope else set()

    # Holds are read BEFORE debits. The two reads are separate statements (READ COMMITTED), and a
    # settle can commit between them: it closes a hold and writes the debit in one transaction.
    # Holds first, that call is seen as held by the first read and as spent by the second, a
    # transient double count (the safe direction for a budget check). Debits first, it is seen as
    # neither, and money in flight vanishes from the answer.
    held = await _held_credits_by_key(session, user_id, all_keys) if all_keys else {}
    per_key = await _debited_by_key(session, user_id, all_keys) if all_keys else {}

    out: dict[str, dict] = {}
    for scope, keys in keys_by_scope.items():
        credits, usd, count = 0, Decimal("0"), 0
        for key in keys:
            if key in per_key:
                k_credits, k_usd, k_count = per_key[key]
                credits += k_credits
                usd += k_usd
                count += k_count
        held_usd = credits_to_usd(sum(held.get(key, 0) for key in keys)).quantize(_USD_PLACES)
        out[scope] = {"credits": credits, "usd": float(usd), "held_usd": float(held_usd), "debit_count": count}
    return out
