"""The chat list: each thread with what it waits on, what it made and what it spent.

``list_threads`` runs a fixed set of grouped statements over the page's thread ids, whatever the page
size: threads, productions, pending cards, routines and, only while an open production has a budget,
the two ledger reads of ``spent_by_scope``. None of them reads a transcript, a log, artifacts or a
breakdown. A card counts as pending for the owner by ``pressable``, the same rule the backend uses to
refuse a retired approval.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from ..models import ChatApproval, ChatProduction, ChatThread, Routine
from ..schemas import ChatThreadListItem, ListedProduction, ListedRoutine
from . import credits_spent
from .chat import LIST_THREADS_MAX, _uuid
from .chat_preview import preview_line

RETIRED_ACTIONS = frozenset({"run.start", "run.continue", "plan.approve", "graph.extend"})


def _is_preview_subject(subject: str) -> bool:
    """``production:<id>|preview:<id>``, as the backend reads a scene preview's subject."""
    head, cut, tail = subject.partition("|")
    production, preview = head.removeprefix("production:"), tail.removeprefix("preview:")
    return bool(cut) and head.startswith("production:") and tail.startswith("preview:") and bool(
        production and preview and "|" not in preview
    )


def pressable(action: str, subject: str, open_production_id: str | None) -> bool:
    """Whether a pending card is one the owner can still press (what Needs you counts)."""
    if action in RETIRED_ACTIONS or subject.startswith("run:"):
        return False
    if action == "frames.approve" and not _is_preview_subject(subject):
        return False  # a director run's frames gate
    if subject.startswith("production:"):
        return open_production_id is not None and subject.startswith(f"production:{open_production_id}|")
    return True


async def _productions(session: AsyncSession, ids: list[str]) -> dict[str, Any]:
    """Per thread, its open production, else its newest closed one: one statement, row 1 of each thread."""
    ranked = (
        select(
            ChatProduction.thread_id,
            ChatProduction.id,
            ChatProduction.status,
            ChatProduction.budget_usd,
            ChatProduction.outcome,
            ChatProduction.closed_at,
            ChatProduction.watches,
            func.row_number()
            .over(
                partition_by=ChatProduction.thread_id,
                order_by=[
                    (ChatProduction.status == "open").desc(),
                    ChatProduction.closed_at.desc().nulls_last(),
                    ChatProduction.created_at.desc(),
                ],
            )
            .label("rank"),
        )
        .where(ChatProduction.thread_id.in_(ids))
        .subquery()
    )
    rows = (await session.execute(select(ranked).where(ranked.c.rank == 1))).all()
    return {row.thread_id: row for row in rows}


async def _pending(session: AsyncSession, ids: list[str], open_ids: dict[str, str]) -> dict[str, list[Any]]:
    """Per thread, its pressable pending cards, oldest first."""
    rows = (
        await session.execute(
            select(
                ChatApproval.thread_id,
                ChatApproval.action,
                ChatApproval.subject,
                ChatApproval.summary,
                ChatApproval.amount_usd,
                ChatApproval.created_at,
            )
            .where(ChatApproval.thread_id.in_(ids), ChatApproval.status == "pending")
            .order_by(ChatApproval.created_at, ChatApproval.id)
        )
    ).all()
    cards: dict[str, list[Any]] = {}
    for row in rows:
        if pressable(row.action, row.subject, open_ids.get(row.thread_id)):
            cards.setdefault(row.thread_id, []).append(row)
    return cards


async def _routines(session: AsyncSession, user_id: str, threads: list[ChatThread]) -> dict[str, ListedRoutine]:
    """The user's routines the page's threads name, by routine id (another user's id finds nothing)."""
    wanted = {rid for t in threads if t.routine_id and (rid := _uuid(t.routine_id))}
    if not wanted:
        return {}
    rows = (
        await session.execute(
            select(
                Routine.id, Routine.cadence, Routine.weekday, Routine.time_of_day, Routine.state,
                Routine.run_count, Routine.last_outcome, Routine.last_error_code, Routine.last_run_at,
            ).where(Routine.id.in_(wanted), Routine.user_id == user_id)
        )
    ).all()
    return {
        row.id: ListedRoutine(
            cadence=row.cadence, weekday=row.weekday, time_of_day=row.time_of_day, state=row.state,
            run_count=row.run_count, last_outcome=row.last_outcome, last_error_code=row.last_error_code,
            last_run_at=row.last_run_at,
        )
        for row in rows
    }


async def _open_spend(session: AsyncSession, user_id: str, budgeted: list[str]) -> dict[str, dict]:
    """The ledger's settled and held USD of each open, budgeted production (two statements, however many)."""
    if not budgeted:
        return {}
    return await credits_spent.spent_by_scope(session, user_id, {pid: [pid] for pid in budgeted})


async def list_threads(
    session: AsyncSession, user_id: str, *, limit: int = 100, offset: int = 0
) -> list[ChatThreadListItem]:
    """Newest activity first. The transcript and known media are not loaded."""
    from .productions import _in_flight  # productions imports chat

    query = (
        select(ChatThread)
        .options(defer(ChatThread.transcript), defer(ChatThread.known_media))
        .where(ChatThread.user_id == user_id)
        .order_by(ChatThread.updated_at.desc(), ChatThread.created_at.desc(), ChatThread.id.desc())
        .limit(max(1, min(limit, LIST_THREADS_MAX)))
        .offset(max(0, offset))
    )
    threads = list((await session.execute(query)).scalars())
    items = [ChatThreadListItem.model_validate(thread) for thread in threads]
    if not threads:
        return items
    ids = [thread.id for thread in threads]
    productions = await _productions(session, ids)
    open_ids = {tid: p.id for tid, p in productions.items() if p.status == "open"}
    cards = await _pending(session, ids, open_ids)
    routines = await _routines(session, user_id, threads)
    spend = await _open_spend(session, user_id, [pid for tid, pid in open_ids.items() if productions[tid].budget_usd > 0])
    for thread, item in zip(threads, items):
        production = productions.get(thread.id)
        if production is not None:
            open_spend = spend.get(production.id)
            item.latest_production = ListedProduction(
                id=production.id,
                status=production.status,
                budget_usd=production.budget_usd,
                settled_usd=open_spend["usd"] if open_spend else None,
                held_usd=open_spend["held_usd"] if open_spend else None,
                outcome=production.outcome,
                closed_at=production.closed_at,
            )
            item.jobs_running = production.status == "open" and any(_in_flight(w) for w in production.watches)
        pending = cards.get(thread.id, [])
        item.pending_count = len(pending)
        item.needs_you = bool(pending)
        if pending:
            newest = pending[-1]
            item.pending_action = newest.action
            item.pending_summary = preview_line(newest.summary)
            item.pending_amount_usd = newest.amount_usd
        item.routine_info = routines.get(_uuid(thread.routine_id)) if thread.routine_id else None
    return items
