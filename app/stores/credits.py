"""Credit ledger + balance operations.

All reserve/settle/release calls are idempotent via `idempotency_key`; the
append-only ledger is enforced by DB triggers (see migration 0010).

The 1.7× markup lives only in `MARKUP` here — the Go backend never multiplies
locally. Registry mirrors this constant via `/pricing/config` and the backend
refuses to boot if they disagree.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from ..models import CreditsLedger, User
from . import users


MARKUP = Decimal(os.getenv("MARKUP", "1.7"))
CREDITS_PER_USD = Decimal(os.getenv("CREDITS_PER_USD", "100"))


def usd_to_credits(actual_cost_usd: Decimal | float | str) -> int:
    """1 credit = $0.01; always ceil after markup so the platform never loses cents."""
    usd = Decimal(str(actual_cost_usd))
    raw = usd * CREDITS_PER_USD * MARKUP
    # ceil via math (Decimal rounding would work too, but math.ceil on float is adequate here)
    return int(math.ceil(float(raw)))


def credits_to_usd(credits: int) -> Decimal:
    if credits <= 0:
        return Decimal("0")
    denominator = CREDITS_PER_USD * MARKUP
    if denominator <= 0:
        return Decimal("0")
    return Decimal(credits) / denominator


@dataclass
class BalanceView:
    balance: int
    reserved: int
    daily_limit: int
    is_admin: bool


async def get_balance(session: AsyncSession, user_id: str) -> Optional[BalanceView]:
    row = await session.execute(
        select(
            User.credits_balance,
            User.credits_reserved,
            User.daily_spend_limit,
            User.is_admin,
        ).where(User.id == user_id)
    )
    r = row.first()
    if r is None:
        return None
    return BalanceView(balance=r[0], reserved=r[1], daily_limit=r[2], is_admin=bool(r[3]))


class CreditsError(Exception):
    """Raised for 402-mappable states (insufficient credits, daily limit, etc.)."""

    def __init__(self, code: str, have: int = 0, need: int = 0, extra: dict | None = None):
        self.code = code
        self.have = have
        self.need = need
        self.extra = extra or {}
        super().__init__(code)


# ── Reserve ─────────────────────────────────────────────────────────────────

_RESERVE_SQL = text(
    """
    UPDATE users
       SET credits_balance  = credits_balance - :amount,
           credits_reserved = credits_reserved + :amount
     WHERE id = :user_id
       AND credits_balance >= :amount
       AND (
         SELECT COALESCE(SUM(-delta), 0)
           FROM credits_ledger
          WHERE user_id = :user_id
            AND kind = 'debit'
            AND created_at > now() - interval '24 hours'
       ) + :amount <= daily_spend_limit
    RETURNING credits_balance, credits_reserved, daily_spend_limit
    """
)

_DIAGNOSE_SQL = text(
    """
    SELECT credits_balance, daily_spend_limit,
           COALESCE((
             SELECT SUM(-delta) FROM credits_ledger
              WHERE user_id = :user_id
                AND kind = 'debit'
                AND created_at > now() - interval '24 hours'
           ), 0) AS spent_today
      FROM users WHERE id = :user_id
    """
)


async def reserve(
    session: AsyncSession,
    *,
    user_id: str,
    amount: int,
    pipeline_run_id: Optional[str],
    checkpoint_id: Optional[str],
    attempt: int,
    idempotency_key: str,
) -> dict:
    if amount <= 0:
        raise CreditsError("invalid_amount")

    # Idempotency fast path.
    existing = await session.execute(
        select(CreditsLedger.id, User.credits_balance, User.credits_reserved)
        .join(User, User.id == CreditsLedger.user_id)
        .where(CreditsLedger.idempotency_key == idempotency_key)
    )
    found = existing.first()
    if found is not None:
        return {"status": "already_reserved", "balance": found[1], "reserved": found[2]}

    result = await session.execute(
        _RESERVE_SQL, {"user_id": user_id, "amount": amount}
    )
    row = result.first()
    if row is None:
        diag = await session.execute(_DIAGNOSE_SQL, {"user_id": user_id})
        d = diag.first()
        if d is None:
            raise CreditsError("user_not_found")
        balance, daily_limit, spent_today = int(d[0]), int(d[1]), int(d[2])
        if balance < amount:
            raise CreditsError("insufficient_credits", have=balance, need=amount)
        if spent_today + amount > daily_limit:
            raise CreditsError(
                "daily_limit_exceeded",
                have=daily_limit - spent_today,
                need=amount,
                extra={"daily_limit": daily_limit, "spent_today": spent_today},
            )
        raise CreditsError("reserve_failed")

    session.add(
        CreditsLedger(
            user_id=user_id,
            kind="hold",
            delta=amount,
            pipeline_run_id=pipeline_run_id,
            checkpoint_id=checkpoint_id,
            attempt=attempt,
            idempotency_key=idempotency_key,
        )
    )
    try:
        await session.commit()
    except IntegrityError:
        # Concurrent reserve with same idempotency_key beat us; roll back our
        # UPDATE (the whole txn) and return the already-reserved view.
        await session.rollback()
        bv = await get_balance(session, user_id)
        return {
            "status": "already_reserved",
            "balance": bv.balance if bv else 0,
            "reserved": bv.reserved if bv else 0,
        }

    return {"status": "reserved", "balance": int(row[0]), "reserved": int(row[1])}


# ── Hold closure ────────────────────────────────────────────────────────────
#
# A hold is keyed by (pipeline_run_id, checkpoint_id, attempt). It stays OPEN —
# its credits sitting in `credits_reserved` — until one of these closes it:
#
#   * a `debit` or `release` row with the same (run, checkpoint_id, attempt).
#     settle() writes a debit (plus a `settle_slack` release); release() writes
#     one release row per hold it returns.
#   * a legacy run-wide `release` row: checkpoint_id NULL, same run, created_at
#     >= the hold's created_at. Before per-hold release rows existed, release()
#     returned every then-open hold of the run under ONE aggregate row with no
#     checkpoint_id, which the key match above can never see. Without this rule
#     those holds looked open forever and every later release refunded them
#     again.
#
# release() refunds exactly the open holds, and settle() of a hold that is no
# longer open must not touch `credits_reserved` again.

_OPEN_HOLDS_SQL = """
    SELECT h.checkpoint_id, h.attempt, SUM(h.delta) AS amount
      FROM credits_ledger h
     WHERE h.user_id = :uid
       AND h.pipeline_run_id = :run_id
       AND h.kind = 'hold'
       {hold_filter}
       AND NOT EXISTS (
             SELECT 1
               FROM credits_ledger c
              WHERE c.user_id = :uid
                AND c.pipeline_run_id = :run_id
                AND c.kind IN ('debit', 'release')
                AND c.checkpoint_id = h.checkpoint_id
                AND c.attempt = h.attempt
           )
       AND NOT EXISTS (
             SELECT 1
               FROM credits_ledger l
              WHERE l.user_id = :uid
                AND l.pipeline_run_id = :run_id
                AND l.kind = 'release'
                AND l.checkpoint_id IS NULL
                AND l.created_at >= h.created_at
           )
     GROUP BY h.checkpoint_id, h.attempt
     ORDER BY MIN(h.created_at), h.checkpoint_id, h.attempt
"""

_OPEN_HOLDS_RUN_SQL = text(_OPEN_HOLDS_SQL.format(hold_filter=""))
_OPEN_HOLDS_ONE_SQL = text(
    _OPEN_HOLDS_SQL.format(
        hold_filter="AND h.checkpoint_id = :checkpoint_id AND h.attempt = :attempt"
    )
)


async def _open_holds(
    session: AsyncSession,
    user_id: str,
    pipeline_run_id: str,
    checkpoint_id: Optional[str] = None,
    attempt: Optional[int] = None,
) -> list[tuple[Optional[str], int, int]]:
    """Open holds of the run as (checkpoint_id, attempt, credits), oldest first.

    With checkpoint_id + attempt, only that one hold (empty list when it is
    closed or never existed).
    """
    params: dict = {"uid": user_id, "run_id": pipeline_run_id}
    if checkpoint_id is not None:
        params.update({"checkpoint_id": checkpoint_id, "attempt": attempt})
        res = await session.execute(_OPEN_HOLDS_ONE_SQL, params)
    else:
        res = await session.execute(_OPEN_HOLDS_RUN_SQL, params)
    return [(r[0], int(r[1]), int(r[2])) for r in res.all()]


async def _lock_user_row(session: AsyncSession, user_id: str) -> None:
    """Serialise settle/release for one user.

    Both read the ledger to decide which holds are open and then move money
    between balance and reserved; two of them interleaving between that read
    and their commit could each act on the same hold (refund it twice, or
    refund it AND subtract it from reserved again at settle). The users row
    lock makes the decision and the write atomic per user. FOR NO KEY UPDATE is
    exactly the lock the later `UPDATE users` takes anyway, only taken earlier —
    it does not block the FK KEY SHARE lock ledger inserts take (no new
    deadlock surface). A no-op on sqlite (tests), which has no row locks.
    """
    await session.execute(
        select(User.id).where(User.id == user_id).with_for_update(key_share=True)
    )


# ── Settle ──────────────────────────────────────────────────────────────────


async def _find_hold(
    session: AsyncSession,
    user_id: str,
    pipeline_run_id: str,
    checkpoint_id: str,
    attempt: int,
) -> Optional[int]:
    res = await session.execute(
        select(CreditsLedger.delta).where(
            CreditsLedger.user_id == user_id,
            CreditsLedger.pipeline_run_id == pipeline_run_id,
            CreditsLedger.checkpoint_id == checkpoint_id,
            CreditsLedger.attempt == attempt,
            CreditsLedger.kind == "hold",
        )
    )
    row = res.first()
    return int(row[0]) if row else None


async def settle(
    session: AsyncSession,
    *,
    user_id: str,
    pipeline_run_id: str,
    checkpoint_id: str,
    attempt: int,
    actual_cost_usd: Decimal | float | str,
    provider: Optional[str],
    model: Optional[str],
    cost_source: Optional[str],
    idempotency_key: str,
) -> dict:
    await _lock_user_row(session, user_id)
    existing = await session.execute(
        select(CreditsLedger.id).where(CreditsLedger.idempotency_key == idempotency_key)
    )
    if existing.first() is not None:
        bv = await get_balance(session, user_id)
        return {
            "status": "already_settled",
            "balance": bv.balance if bv else 0,
            "reserved": bv.reserved if bv else 0,
        }

    hold = await _find_hold(session, user_id, pipeline_run_id, checkpoint_id, attempt)
    if hold is None:
        raise CreditsError("no_matching_hold")
    # A release (per-hold, or a run-wide one that caught this call in flight)
    # may already have returned the hold to balance and taken it out of
    # reserved. Settling it like an open hold would subtract it from reserved a
    # second time and refund its slack a second time.
    hold_open = bool(
        await _open_holds(session, user_id, pipeline_run_id, checkpoint_id, attempt)
    )

    actual_cost_decimal = Decimal(str(actual_cost_usd))
    actual_credits = usd_to_credits(actual_cost_decimal)
    resolved_cost_source = (cost_source or "").strip() or None
    if actual_credits <= 0:
        # Missing provider telemetry: settle using the held estimate so the
        # checkpoint is never free while still preserving idempotent accounting.
        actual_credits = hold
        actual_cost_decimal = credits_to_usd(actual_credits)
        if resolved_cost_source is None:
            resolved_cost_source = "registry_estimate"
    bv = await get_balance(session, user_id)
    if bv is None:
        raise CreditsError("user_not_found")

    balance_exhausted = False
    # What leaves credits_reserved: the hold, unless a release already took it
    # out (and gave it back to balance).
    reserved_release = hold if hold_open else 0

    if not hold_open:
        # The whole hold is already back in balance, so there is no slack to
        # refund: the actual cost comes out of balance alone, as far as the
        # balance covers it.
        covered = min(actual_credits, max(bv.balance, 0))
        balance_delta = -covered
        debited_delta = -covered
        extra_rows = []
        if covered < actual_credits:
            extra_rows.append(
                CreditsLedger(
                    user_id=user_id,
                    kind="adjust",
                    delta=-(actual_credits - covered),
                    pipeline_run_id=pipeline_run_id,
                    checkpoint_id=checkpoint_id,
                    attempt=attempt,
                    note="estimate_shortfall",
                )
            )
            balance_exhausted = True
    elif hold >= actual_credits:
        slack = hold - actual_credits
        balance_delta = slack
        debited_delta = -actual_credits
        extra_rows = []
        if slack > 0:
            extra_rows.append(
                CreditsLedger(
                    user_id=user_id,
                    kind="release",
                    delta=slack,
                    pipeline_run_id=pipeline_run_id,
                    checkpoint_id=checkpoint_id,
                    attempt=attempt,
                    note="settle_slack",
                )
            )
    else:
        shortfall = actual_credits - hold
        if bv.balance >= shortfall:
            balance_delta = -shortfall
            debited_delta = -actual_credits
            extra_rows = []
        else:
            covered_extra = bv.balance
            balance_delta = -covered_extra
            debited_delta = -(hold + covered_extra)
            uncovered = actual_credits - hold - covered_extra
            extra_rows = [
                CreditsLedger(
                    user_id=user_id,
                    kind="adjust",
                    delta=-uncovered,
                    pipeline_run_id=pipeline_run_id,
                    checkpoint_id=checkpoint_id,
                    attempt=attempt,
                    note="estimate_shortfall",
                )
            ]
            balance_exhausted = True

    # Relative, race-safe mutation: two concurrent settles for the same user
    # each read the same snapshot, but the deltas compose in SQL so neither
    # clobbers the other's balance refund or reserved release. RETURNING gives
    # the authoritative post-write totals for the response.
    updated = await session.execute(
        text(
            """
            UPDATE users
               SET credits_balance  = credits_balance + :balance_delta,
                   credits_reserved = credits_reserved - :reserved_release
             WHERE id = :uid
            RETURNING credits_balance, credits_reserved
            """
        ),
        {
            "balance_delta": balance_delta,
            "reserved_release": reserved_release,
            "uid": user_id,
        },
    )
    new_row = updated.first()
    new_balance = int(new_row[0])
    new_reserved = int(new_row[1])

    call_fields = dict(
        provider=provider,
        model=model,
        cost_usd=actual_cost_decimal,
        cost_source=resolved_cost_source,
        idempotency_key=idempotency_key,
    )
    if debited_delta < 0:
        session.add(
            CreditsLedger(
                user_id=user_id,
                kind="debit",
                delta=debited_delta,
                pipeline_run_id=pipeline_run_id,
                checkpoint_id=checkpoint_id,
                attempt=attempt,
                **call_fields,
            )
        )
    else:
        # Only a released hold settling against an empty balance debits
        # nothing, and the ledger forbids a zero debit: the shortfall row
        # carries the call's cost and idempotency key instead, so a replay
        # still answers already_settled.
        for field, value in call_fields.items():
            setattr(extra_rows[0], field, value)
    for r in extra_rows:
        session.add(r)

    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        bv2 = await get_balance(session, user_id)
        return {
            "status": "already_settled",
            "balance": bv2.balance if bv2 else 0,
            "reserved": bv2.reserved if bv2 else 0,
        }

    if balance_exhausted:
        raise CreditsError(
            "balance_exhausted",
            have=0,
            need=actual_credits,
            extra={"debited": -debited_delta, "uncovered": actual_credits + debited_delta},
        )

    return {"status": "settled", "balance": new_balance, "reserved": new_reserved}


# ── Release ─────────────────────────────────────────────────────────────────


def _hold_release_key(idempotency_key: str, checkpoint_id: Optional[str], attempt: int) -> str:
    """Idempotency key for the 2nd+ release row a run-wide release writes.

    The caller's key sits on the first row (so a replay still answers
    already_released); the others need their own non-null, unique keys.
    """
    return f"{idempotency_key}#{checkpoint_id or ''}#{attempt}"


async def release(
    session: AsyncSession,
    *,
    user_id: str,
    pipeline_run_id: str,
    reason: str,
    idempotency_key: str,
    checkpoint_id: Optional[str] = None,
    attempt: Optional[int] = None,
) -> dict:
    """Return open holds of a run to balance.

    With checkpoint_id + attempt: only that hold, if still open. Without: every
    open hold of the run (the old backend's run-wide release). Either way each
    returned hold gets its OWN release row carrying its checkpoint_id/attempt,
    so no later release (or settle) can return it again.
    """
    if (checkpoint_id is None) != (attempt is None):
        raise ValueError("checkpoint_id and attempt go together")

    await _lock_user_row(session, user_id)
    existing = await session.execute(
        select(CreditsLedger.id).where(CreditsLedger.idempotency_key == idempotency_key)
    )
    if existing.first() is not None:
        bv = await get_balance(session, user_id)
        return {
            "status": "already_released",
            "balance": bv.balance if bv else 0,
            "reserved": bv.reserved if bv else 0,
        }

    open_holds = await _open_holds(
        session, user_id, pipeline_run_id, checkpoint_id, attempt
    )
    to_return = sum(amount for _, _, amount in open_holds)

    if to_return <= 0:
        bv = await get_balance(session, user_id)
        return {
            "status": "nothing_to_release",
            "balance": bv.balance if bv else 0,
            "reserved": bv.reserved if bv else 0,
        }

    await session.execute(
        text(
            """
            UPDATE users
               SET credits_balance  = credits_balance + :amt,
                   credits_reserved = credits_reserved - :amt
             WHERE id = :uid
            """
        ),
        {"amt": to_return, "uid": user_id},
    )

    for i, (hold_checkpoint_id, hold_attempt, amount) in enumerate(open_holds):
        session.add(
            CreditsLedger(
                user_id=user_id,
                kind="release",
                delta=amount,
                pipeline_run_id=pipeline_run_id,
                checkpoint_id=hold_checkpoint_id,
                attempt=hold_attempt,
                note=reason,
                idempotency_key=(
                    idempotency_key
                    if i == 0
                    else _hold_release_key(idempotency_key, hold_checkpoint_id, hold_attempt)
                ),
            )
        )
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        bv = await get_balance(session, user_id)
        return {
            "status": "already_released",
            "balance": bv.balance if bv else 0,
            "reserved": bv.reserved if bv else 0,
        }

    bv = await get_balance(session, user_id)
    return {
        "status": "released",
        "balance": bv.balance if bv else 0,
        "reserved": bv.reserved if bv else 0,
        "returned": to_return,
    }


# ── Admin ops ───────────────────────────────────────────────────────────────


async def grant(
    session: AsyncSession,
    *,
    admin_user_id: str,
    target_user_id: str | None = None,
    target_username: str | None = None,
    amount: int,
    note: str,
) -> dict:
    if amount <= 0:
        raise CreditsError("invalid_amount")

    resolved_user = None
    if target_username:
        resolved_user = await users.get_user_by_username(session, target_username)
    elif target_user_id:
        resolved_user = await users.get_user_by_id(session, target_user_id)

    if resolved_user is None:
        raise CreditsError("user_not_found")

    target_user_id = resolved_user.id

    await session.execute(
        text("UPDATE users SET credits_balance = credits_balance + :a WHERE id = :uid"),
        {"a": amount, "uid": target_user_id},
    )
    session.add(
        CreditsLedger(
            user_id=target_user_id,
            kind="grant",
            delta=amount,
            admin_user_id=admin_user_id,
            note=note,
        )
    )
    await session.commit()
    bv = await get_balance(session, target_user_id)
    return {
        "status": "granted",
        "user_id": target_user_id,
        "username": resolved_user.username,
        "balance": bv.balance if bv else 0,
        "reserved": bv.reserved if bv else 0,
    }


async def ledger(
    session: AsyncSession,
    *,
    user_id: Optional[str] = None,
    username: Optional[str] = None,
    since: Optional[str] = None,
    limit: int = 100,
) -> list[dict]:
    ledger_user = aliased(User)
    admin_user = aliased(User)
    stmt = select(
        CreditsLedger.id,
        CreditsLedger.user_id,
        ledger_user.username,
        CreditsLedger.kind,
        CreditsLedger.delta,
        CreditsLedger.pipeline_run_id,
        CreditsLedger.checkpoint_id,
        CreditsLedger.attempt,
        CreditsLedger.provider,
        CreditsLedger.model,
        CreditsLedger.cost_usd,
        CreditsLedger.cost_source,
        CreditsLedger.note,
        CreditsLedger.admin_user_id,
        admin_user.username,
        CreditsLedger.created_at,
    ).join(
        ledger_user,
        ledger_user.id == CreditsLedger.user_id,
    ).outerjoin(
        admin_user,
        admin_user.id == CreditsLedger.admin_user_id,
    ).order_by(CreditsLedger.created_at.desc())
    if user_id:
        stmt = stmt.where(CreditsLedger.user_id == user_id)
    if username:
        stmt = stmt.where(ledger_user.username == username)
    if since:
        stmt = stmt.where(CreditsLedger.created_at >= since)
    stmt = stmt.limit(limit)
    res = await session.execute(stmt)
    out = []
    for r in res.all():
        out.append(
            {
                "id": r[0],
                "user_id": r[1],
                "user_username": r[2],
                "kind": r[3],
                "delta": int(r[4]),
                "pipeline_run_id": r[5],
                "checkpoint_id": r[6],
                "attempt": r[7],
                "provider": r[8],
                "model": r[9],
                "cost_usd": float(r[10]) if r[10] is not None else None,
                "cost_source": r[11],
                "note": r[12],
                "admin_user_id": r[13],
                "admin_username": r[14],
                "created_at": r[15].isoformat() if r[15] else None,
            }
        )
    return out


async def is_admin(session: AsyncSession, user_id: str) -> bool:
    res = await session.execute(select(User.is_admin).where(User.id == user_id))
    row = res.first()
    return bool(row[0]) if row else False
