"""Chat productions: the durable work item of a chat thread (migration 0045).

Every function is scoped by ``user_id`` as ``chat.py`` is: another user's row is simply not
found (``None``), and a refusal the caller must tell apart raises ``ChatError`` carrying the
HTTP status and a stable code. The sweeps that run without a user (``pending_wakes``,
``watching_clip``, ``watching_media``) are the exception: the internal secret is their gate.

A PATCH is one write. The row is locked (``SELECT ... FOR UPDATE``), the refusals are decided
on it, and a single UPDATE applies every field: money moves by SQL increments, the JSON
columns are rebuilt from the locked row, and the budget-version check sits in the UPDATE's own
WHERE clause so a budget card can never be applied twice.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import ChatProduction
from ..schemas import ProductionIn, ProductionPatch
from .chat import ChatError, _now, _owned_thread, _usd, _uuid

LOG_KEEP = 200
LIST_MAX = 50


async def create_production(
    session: AsyncSession, user_id: str, thread_id: str, payload: ProductionIn
) -> ChatProduction | None:
    """Open a production on the user's thread; ``None`` when the thread is not theirs.
    A thread with an open production refuses with 409 ``production_open``."""
    thread = await _owned_thread(session, user_id, thread_id)
    if thread is None:
        return None
    tid = thread.id
    row = ChatProduction(
        id=str(uuid.uuid4()),
        thread_id=tid,
        user_id=user_id,
        title=payload.title,
        goal=payload.goal,
        playbook_slug=payload.playbook_slug,
        agent_cap_usd=payload.agent_cap_usd,
    )
    session.add(row)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        # The partial unique index refused: two requests raced to open the thread's production.
        if await get_open_production(session, user_id, tid) is not None:
            raise ChatError(409, "production_open")
        raise
    await session.refresh(row)
    return row


async def get_open_production(session: AsyncSession, user_id: str, thread_id: str) -> ChatProduction | None:
    tid = _uuid(thread_id)
    if tid is None:
        return None
    return (
        await session.execute(
            select(ChatProduction).where(
                ChatProduction.thread_id == tid, ChatProduction.user_id == user_id, ChatProduction.status == "open"
            )
        )
    ).scalar_one_or_none()


async def get_production(session: AsyncSession, user_id: str, production_id: str) -> ChatProduction | None:
    pid = _uuid(production_id)
    if pid is None:
        return None
    return (
        await session.execute(
            select(ChatProduction).where(ChatProduction.id == pid, ChatProduction.user_id == user_id)
        )
    ).scalar_one_or_none()


async def list_closed(session: AsyncSession, user_id: str, *, limit: int = 10) -> list[ChatProduction]:
    """The user's closed productions, newest first."""
    query = (
        select(ChatProduction)
        .where(ChatProduction.user_id == user_id, ChatProduction.status == "closed")
        .order_by(ChatProduction.closed_at.desc(), ChatProduction.created_at.desc(), ChatProduction.id.desc())
        .limit(max(1, min(limit, LIST_MAX)))
    )
    return list((await session.execute(query)).scalars())


def _in_flight(watch: dict[str, Any]) -> bool:
    """A job that has not ended: a render shown rendering or a media job shown running."""
    return watch.get("status") == "rendering" or (watch.get("kind") == "media" and watch.get("status") == "running")


async def pending_wakes(session: AsyncSession) -> list[ChatProduction]:
    """Open productions of every user the boot has to look at: a wake-up the agent has not
    handled yet, or a render or media job still shown in flight. A restart cuts a job off with
    nothing owed yet, and nothing else would ever end its watch. As in ``watching_clip``, the
    open rows are few and the watch is matched here rather than with a dialect-specific JSONB
    query."""
    query = (
        select(ChatProduction)
        .where(ChatProduction.status == "open")
        .order_by(ChatProduction.created_at, ChatProduction.id)
    )
    return [
        row for row in (await session.execute(query)).scalars()
        if row.wake_seq > row.acked_seq or any(_in_flight(watch) for watch in row.watches)
    ]


async def _open_waiting(session: AsyncSession, waiting: dict[str, str]) -> list[ChatProduction]:
    """Open productions of every user with a watch that has all of ``waiting``. A thread has at
    most one open production, so the open rows are few and the watch is matched here rather
    than with a dialect-specific JSONB query."""
    query = (
        select(ChatProduction)
        .where(ChatProduction.status == "open")
        .order_by(ChatProduction.created_at, ChatProduction.id)
    )
    return [
        row for row in (await session.execute(query)).scalars()
        if any(waiting.items() <= watch.items() for watch in row.watches)
    ]


async def watching_clip(session: AsyncSession, clip_id: str) -> list[ChatProduction]:
    """Open productions of every user that wait on this clip's render."""
    return await _open_waiting(session, {"kind": "render", "clip_id": clip_id, "status": "rendering"})


async def watching_media(session: AsyncSession, media_id: str) -> list[ChatProduction]:
    """Open productions of every user that wait on this media item's background job."""
    return await _open_waiting(session, {"kind": "media", "media_id": media_id, "status": "running"})


def _refusal(row: ChatProduction, patch: ProductionPatch) -> ChatError | None:
    """Why the locked row refuses this patch, or None."""
    changes = {name for name in ProductionPatch.model_fields if getattr(patch, name) is not None}
    if row.status == "closed" and changes - {"acked_seq"}:
        return ChatError(409, "production_closed")
    if patch.expect_budget_version is not None and patch.expect_budget_version != row.budget_version:
        return ChatError(409, "stale_budget_version")
    wake_seq = row.wake_seq + (1 if patch.bump_wake is not None else 0)
    if patch.acked_seq is not None and patch.acked_seq > wake_seq:
        return ChatError(422, "ack_beyond_wake")
    return None


def _artifacts(row: ChatProduction, patch: ProductionPatch, now: datetime) -> dict[str, Any]:
    """The artifacts column as the locked ``row`` holds it, with the patch's additions. It is
    rebuilt whole, so the keys the patch does not touch are carried over, and the try counts are
    read and written under the same row lock as every other patch (no two increments are lost)."""
    current = row.artifacts
    artifacts = {
        **current,
        "media_ids": list(dict.fromkeys([*current.get("media_ids", []), *(patch.add_media_ids or [])])),
        "clip_ids": list(dict.fromkeys([*current.get("clip_ids", []), *(patch.add_clip_ids or [])])),
    }
    if patch.add_approved_scenes:
        scenes = list(current.get("approved_scenes", []))
        known = {scene["key"] for scene in scenes}
        for scene in patch.add_approved_scenes:
            if scene.key not in known:
                known.add(scene.key)
                stamped = scene.model_copy(update={"approved_at": scene.approved_at or now})
                scenes.append(stamped.model_dump(mode="json"))
        artifacts["approved_scenes"] = scenes
    if patch.add_tries:
        tries = dict(current.get("tries", {}))
        for key, amount in patch.add_tries.items():
            tries[key] = max(0, tries.get(key, 0) + amount)  # a refund never goes below 0
        artifacts["tries"] = tries
    return artifacts


def _values(row: ChatProduction, patch: ProductionPatch, now: datetime) -> dict[str, Any]:
    """The UPDATE's SET list: increments as SQL expressions, JSON columns rebuilt from ``row``."""
    values: dict[str, Any] = {}
    if patch.add_budget_usd is not None:
        values["budget_usd"] = ChatProduction.budget_usd + _usd(patch.add_budget_usd)
        values["budget_version"] = ChatProduction.budget_version + 1
    if patch.add_agent_cap_usd is not None:
        values["agent_cap_usd"] = ChatProduction.agent_cap_usd + _usd(patch.add_agent_cap_usd)
    if patch.add_agent_spent_usd is not None:
        values["agent_spent_usd"] = ChatProduction.agent_spent_usd + _usd(patch.add_agent_spent_usd)
    if patch.append_log:
        entries = [{"at": now.isoformat(), **entry.model_dump()} for entry in patch.append_log]
        values["log"] = [*row.log, *entries][-LOG_KEEP:]
    if patch.add_media_ids or patch.add_clip_ids or patch.add_approved_scenes or patch.add_tries:
        values["artifacts"] = _artifacts(row, patch, now)
    if patch.watches is not None:
        values["watches"] = [watch.model_dump(mode="json") for watch in patch.watches]
    if patch.bump_wake is not None:
        values["wake_seq"] = ChatProduction.wake_seq + 1
        values["wake_reason"] = patch.bump_wake.reason
    if patch.acked_seq is not None:
        values["acked_seq"] = max(row.acked_seq, patch.acked_seq)  # an ack never moves back
    if patch.close is not None:
        values.update(status="closed", outcome=patch.close.outcome, closed_at=now)
    return values


async def patch_production(
    session: AsyncSession, user_id: str, production_id: str, patch: ProductionPatch
) -> ChatProduction | None:
    """Apply every field of ``patch`` in one write; ``None`` when the row is not the user's."""
    pid = _uuid(production_id)
    if pid is None:
        return None
    row = (
        await session.execute(
            select(ChatProduction)
            .where(ChatProduction.id == pid, ChatProduction.user_id == user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    if (refusal := _refusal(row, patch)) is not None:
        await session.rollback()  # frees the row lock
        raise refusal
    now = _now()
    values = _values(row, patch, now)
    if not values:
        await session.commit()  # nothing to write; frees the row lock and keeps the row loaded
        return row
    # The WHERE clause repeats what _refusal decided, so the UPDATE refuses by itself too.
    guards = [ChatProduction.id == pid]
    if patch.expect_budget_version is not None:
        guards.append(ChatProduction.budget_version == patch.expect_budget_version)
    if set(values) - {"acked_seq"}:
        guards.append(ChatProduction.status == "open")
    result = await session.execute(
        update(ChatProduction)
        .where(*guards)
        .values(updated_at=now, **values)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount == 0:
        await session.rollback()
        raise ChatError(409, "stale_budget_version" if patch.expect_budget_version is not None else "production_closed")
    await session.commit()
    await session.refresh(row)
    return row
