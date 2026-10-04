"""What a user actually paid, summed per caller-defined scope.

A "scope" is a set of ledger keys (`credits_ledger.pipeline_run_id`) the
caller groups under one name — the backend asks for one per clip: the clip's
own id (the key its clip-side generations reserve under) plus the pipeline
runs that produced it. Read-only: it never touches balance or holds.

Only `debit` rows count. A debit is what settle took from the user — credits
with the markup already applied, and the raw provider USD beside it — so the
sum is the bill, not a re-pricing. Holds still open are not spent yet,
releases were refunded, and an `adjust` shortfall is a debt the balance did
not cover, not money paid.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CreditsLedger


def _ledger_key(raw: str) -> str | None:
    """The canonical uuid string, or None for anything the UUID column could
    not hold (it would fail the whole query on Postgres)."""
    try:
        return str(uuid.UUID(str(raw).strip()))
    except (ValueError, AttributeError, TypeError):
        return None


async def spent_by_scope(
    session: AsyncSession, user_id: str, scopes: dict[str, list[str]]
) -> dict[str, dict]:
    """{scope: {credits, usd, debit_count}} over the user's debits keyed by
    any of the scope's ledger keys. A key shared by two scopes counts in
    both (each scope is answered on its own); a scope with no debits answers
    zeros. Another user's rows never match: user_id is part of the filter."""
    keys_by_scope = {
        scope: {k for k in (_ledger_key(raw) for raw in raws) if k}
        for scope, raws in scopes.items()
    }
    all_keys = set().union(*keys_by_scope.values()) if keys_by_scope else set()

    per_key: dict[str, tuple[int, Decimal, int]] = {}
    if all_keys:
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
                CreditsLedger.pipeline_run_id.in_(sorted(all_keys)),
            )
            .group_by(CreditsLedger.pipeline_run_id)
        )
        for key, credits, usd, count in rows.all():
            per_key[str(key)] = (int(credits), Decimal(str(usd)), int(count))

    out: dict[str, dict] = {}
    for scope, keys in keys_by_scope.items():
        credits, usd, count = 0, Decimal("0"), 0
        for key in keys:
            if key in per_key:
                k_credits, k_usd, k_count = per_key[key]
                credits += k_credits
                usd += k_usd
                count += k_count
        out[scope] = {"credits": credits, "usd": float(usd), "debit_count": count}
    return out
