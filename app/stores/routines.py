"""Routines: a user's scheduled prompts, their daily spend counter, the sweeper's claim (migration 0050).

Every per-user function is scoped by ``user_id`` (another user's row is simply not found). The
sweeper functions (``due``, ``claim``, ``record_outcome``) are cross-user: the route that calls them
sits behind the internal secret only. The spend counter's ``reserve`` is one transaction: it takes
the day row's lock, so two reserves cannot both be granted the same budget.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import ChatProduction, ChatThread, Routine, RoutineSpendDay
from ..schemas import ROUTINE_LIMIT, RoutineClaimIn, RoutineIn, RoutineOutcomeIn, RoutinePatch
from .chat import _now as _utc_now, _uuid
from .credits import _lock_user_row

PAUSE_AFTER_FAILURES = 3
DUE_MAX = 200
_QUANT = Decimal("0.0001")


class RoutineError(Exception):
    """A refusal with its HTTP status and a fixed detail."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _usd(value: float | Decimal) -> Decimal:
    return Decimal(str(value)).quantize(_QUANT)


async def _owned(session: AsyncSession, user_id: str, routine_id: str) -> Routine | None:
    try:
        rid = str(uuid.UUID(routine_id))
    except (ValueError, AttributeError, TypeError):
        return None
    return (
        await session.execute(select(Routine).where(Routine.id == rid, Routine.user_id == user_id))
    ).scalar_one_or_none()


async def create(session: AsyncSession, user_id: str, payload: RoutineIn) -> Routine:
    await _lock_user_row(session, user_id)
    held = len((await session.execute(select(Routine.id).where(Routine.user_id == user_id))).all())
    if held >= ROUTINE_LIMIT:
        raise RoutineError(409, "routine_limit")
    row = Routine(
        id=str(uuid.uuid4()),
        user_id=user_id,
        name=payload.name,
        prompt=payload.prompt,
        cadence=payload.cadence,
        weekday=payload.weekday,
        time_of_day=payload.time_of_day,
        time_zone=payload.time_zone,
        thread_id=payload.thread_id,
        next_run_at=payload.next_run_at,
    )
    session.add(row)
    if payload.thread_id is not None:
        thread = (
            await session.execute(
                select(ChatThread).where(ChatThread.id == payload.thread_id, ChatThread.user_id == user_id)
            )
        ).scalar_one_or_none()
        if thread is None:
            await session.rollback()
            raise RoutineError(404, "thread_not_found")
        # Same transaction as the insert; updated_at is left alone (the thread was not touched).
        await session.execute(
            update(ChatThread)
            .where(ChatThread.id == thread.id)
            .values(routine_id=row.id, updated_at=ChatThread.updated_at)
            .execution_options(synchronize_session=False)
        )
    await session.commit()
    await session.refresh(row)
    return row


async def list_for_user(session: AsyncSession, user_id: str) -> list[Routine]:
    return list(
        (
            await session.execute(
                select(Routine).where(Routine.user_id == user_id).order_by(Routine.created_at, Routine.id)
            )
        ).scalars()
    )


async def get(session: AsyncSession, user_id: str, routine_id: str) -> Routine | None:
    return await _owned(session, user_id, routine_id)


async def patch(session: AsyncSession, user_id: str, routine_id: str, payload: RoutinePatch) -> Routine | None:
    row = await _owned(session, user_id, routine_id)
    if row is None:
        return None
    if payload.state is not None:
        if payload.state == "active":
            row.consecutive_failures = 0
        row.state = payload.state
    if payload.next_run_at is not None:
        row.next_run_at = payload.next_run_at
    await session.commit()
    await session.refresh(row)
    return row


async def delete(session: AsyncSession, user_id: str, routine_id: str) -> bool:
    row = await _owned(session, user_id, routine_id)
    if row is None:
        return False
    rid = row.id
    # The thread outlives its routine; only the link goes.
    await session.execute(
        update(ChatThread)
        .where(ChatThread.user_id == user_id, ChatThread.routine_id == rid)
        .values(routine_id=None, updated_at=ChatThread.updated_at)
        .execution_options(synchronize_session=False)
    )
    thread_id = _uuid(row.thread_id)
    if thread_id is not None:
        # Its budget (the open production with an end date) ends with it; an ordinary production stays.
        now = _utc_now()
        await session.execute(
            update(ChatProduction)
            .where(
                ChatProduction.thread_id == thread_id, ChatProduction.user_id == user_id,
                ChatProduction.status == "open", ChatProduction.ends_at.is_not(None),
            )
            .values(status="closed", outcome="routine_deleted", closed_at=now, updated_at=now)
            .execution_options(synchronize_session=False)
        )
    await session.delete(row)
    await session.commit()
    return True


# ── the sweeper ──────────────────────────────────────────────────────────────


async def due(session: AsyncSession, now: datetime) -> list[Routine]:
    return list(
        (
            await session.execute(
                select(Routine)
                .where(Routine.state == "active", Routine.next_run_at <= now)
                .order_by(Routine.next_run_at, Routine.id)
                .limit(DUE_MAX)
            )
        ).scalars()
    )


async def claim(session: AsyncSession, routine_id: str, payload: RoutineClaimIn) -> Routine:
    """One compare-and-set: it wins only while the row is active and still due at ``due_at``."""
    res = await session.execute(
        update(Routine)
        .where(Routine.id == routine_id, Routine.state == "active", Routine.next_run_at == payload.due_at)
        .values(next_run_at=payload.next_run_at, last_run_at=payload.due_at)
        .returning(Routine.id)
        .execution_options(synchronize_session=False)
    )
    won = res.scalar_one_or_none()
    await session.commit()
    if won is None:
        raise RoutineError(409, "claim_lost")
    row = (await session.execute(select(Routine).where(Routine.id == routine_id))).scalar_one()
    await session.refresh(row)
    return row


async def record_outcome(session: AsyncSession, routine_id: str, payload: RoutineOutcomeIn) -> Routine | None:
    try:
        rid = str(uuid.UUID(routine_id))
    except (ValueError, AttributeError, TypeError):
        return None
    row = (
        await session.execute(select(Routine).where(Routine.id == rid).with_for_update())
    ).scalar_one_or_none()
    if row is None:
        return None
    row.last_run_at = payload.at
    row.last_outcome = payload.outcome
    row.last_error_code = payload.error_code
    if payload.outcome != "skipped":
        row.run_count += 1  # a skipped wake is not a run
    if payload.outcome == "failed":
        row.consecutive_failures += 1
        if row.consecutive_failures >= PAUSE_AFTER_FAILURES:
            row.state = "paused"
    else:
        row.consecutive_failures = 0
    await session.commit()
    await session.refresh(row)
    return row


# ── the daily spend counter ──────────────────────────────────────────────────


async def _locked_day(session: AsyncSession, user_id: str, day: date) -> RoutineSpendDay:
    """The day's row, created at 0 if absent, with its row lock held until the commit."""
    query = (
        select(RoutineSpendDay)
        .where(RoutineSpendDay.user_id == user_id, RoutineSpendDay.day == day)
        .with_for_update()
    )
    row = (await session.execute(query)).scalar_one_or_none()
    if row is None:
        try:
            async with session.begin_nested():
                session.add(RoutineSpendDay(user_id=user_id, day=day, spent_usd=Decimal(0)))
        except IntegrityError:
            pass  # a concurrent caller made it first
        row = (await session.execute(query.execution_options(populate_existing=True))).scalar_one()
    return row


async def spent(session: AsyncSession, user_id: str, day: date) -> Decimal:
    value = (
        await session.execute(
            select(RoutineSpendDay.spent_usd).where(RoutineSpendDay.user_id == user_id, RoutineSpendDay.day == day)
        )
    ).scalar_one_or_none()
    return Decimal(0) if value is None else _usd(value)


async def add_spend(session: AsyncSession, user_id: str, day: date, add_usd: float) -> Decimal:
    """Add (or, negative, take back) spend; the total never goes below 0."""
    row = await _locked_day(session, user_id, day)
    row.spent_usd = max(Decimal(0), _usd(row.spent_usd) + _usd(add_usd))
    total = _usd(row.spent_usd)
    await session.commit()
    return total


async def reserve_spend(
    session: AsyncSession, user_id: str, day: date, want_usd: float, budget_usd: float
) -> Decimal:
    """Grant ``max(0, min(want, budget - spent))`` and add it to ``spent``, under the day row's lock."""
    row = await _locked_day(session, user_id, day)
    current = _usd(row.spent_usd)
    granted = max(Decimal(0), min(_usd(want_usd), _usd(budget_usd) - current))
    row.spent_usd = current + granted
    await session.commit()
    return granted
