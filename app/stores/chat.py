"""Operator chat: threads, messages and one-time spend approvals.

Every function is scoped by ``user_id``: a thread belongs to its owner, messages and
approvals belong to their thread, and another user's row is simply not found (the route
answers 404, never 403). ``None`` / ``False`` means not found; a refusal that the caller
must tell apart raises ``ChatError`` carrying the HTTP status and a stable code.

An approval is created ``pending``. The Approve button mints a token in the browser; the
backend sends only its sha256 to ``approve_approval``. ``consume_approval`` is what a
spend tool calls with the hash of the token it was handed: it turns ``approved`` into
``used`` once, for the thread, action and subject it was approved for, and says why when
it refuses. The hash is a credential, so it is never returned by any read.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from ..models import ChatApproval, ChatMessage, ChatThread
from ..schemas import (
    ChatApprovalApprove,
    ChatApprovalConsume,
    ChatApprovalIn,
    ChatMessageIn,
    ChatMessagePatch,
    ChatThreadIn,
    ChatThreadPatch,
)

LIST_THREADS_MAX = 200
LIST_MESSAGES_DEFAULT = 500
LIST_MESSAGES_MAX = 1000

# A consume may need this much more than the approved amount (rounding of a price estimate).
AMOUNT_TOLERANCE_USD = Decimal("0.005")

# Concurrent writers that both computed max(seq)+1: the unique (thread, seq) refuses the
# loser, which recomputes. More than a handful of lost races in a row means something is wrong.
_SEQ_ATTEMPTS = 5


class ChatError(Exception):
    """Domain refusal carrying an HTTP status; routes translate it."""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    """sqlite hands timestamps back naive; they were stored as UTC."""
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _usd(value: float) -> Decimal:
    """What goes to a NUMERIC column: a Decimal, so no driver has to guess at a float."""
    return Decimal(str(value))


def _uuid(value: str | None) -> str | None:
    """The canonical id, or None for text that is not a UUID (which cannot name a row,
    and must not reach a uuid column, where postgres would raise instead of not finding)."""
    try:
        return str(uuid.UUID(value))  # type: ignore[arg-type]
    except (ValueError, AttributeError, TypeError):
        return None


# ── threads ──────────────────────────────────────────────────────────────────


async def _insert_once(
    session: AsyncSession, row: Any, *, owned_by_caller: Callable[[Any], bool], taken: str
) -> Any:
    """Insert a row with a caller-minted id, idempotently.

    Two POSTs of the same id race to the primary key: the loser rolls back and gets the
    winner's row when it is the caller's own (a retry), and ``taken`` when it is not.
    """
    model, row_id = type(row), row.id
    session.add(row)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        existing = await session.get(model, row_id)
        if existing is None:
            raise  # not an id collision (a missing owner, say)
        if owned_by_caller(existing):
            return existing
        raise ChatError(409, taken)
    await session.refresh(row)
    return row


async def _owned_thread(session: AsyncSession, user_id: str, thread_id: str) -> ChatThread | None:
    tid = _uuid(thread_id)
    if tid is None:
        return None
    return (
        await session.execute(select(ChatThread).where(ChatThread.id == tid, ChatThread.user_id == user_id))
    ).scalar_one_or_none()


async def create_thread(session: AsyncSession, user_id: str, payload: ChatThreadIn) -> ChatThread:
    # Idempotent on a caller-minted id: a retried POST returns the thread already stored.
    # Another user's id is a conflict, never a read of that row.
    row = ChatThread(
        id=payload.id or str(uuid.uuid4()),
        user_id=user_id,
        title=payload.title,
        provider=payload.provider,
        model=payload.model,
        series_id=payload.series_id,
        transcript=list(payload.transcript),
        known_media=list(payload.known_media),
    )
    return await _insert_once(
        session, row, owned_by_caller=lambda existing: existing.user_id == user_id, taken="thread_id_taken"
    )


async def list_threads(
    session: AsyncSession, user_id: str, *, limit: int = 100, offset: int = 0
) -> list[ChatThread]:
    """Newest activity first. The transcript and known media are not loaded."""
    query = (
        select(ChatThread)
        .options(defer(ChatThread.transcript), defer(ChatThread.known_media))
        .where(ChatThread.user_id == user_id)
        .order_by(ChatThread.updated_at.desc(), ChatThread.created_at.desc(), ChatThread.id.desc())
        .limit(max(1, min(limit, LIST_THREADS_MAX)))
        .offset(max(0, offset))
    )
    return list((await session.execute(query)).scalars())


async def get_thread(session: AsyncSession, user_id: str, thread_id: str) -> ChatThread | None:
    return await _owned_thread(session, user_id, thread_id)


async def patch_thread(
    session: AsyncSession, user_id: str, thread_id: str, patch: ChatThreadPatch
) -> ChatThread | None:
    row = await _owned_thread(session, user_id, thread_id)
    if row is None:
        return None
    for field in ("title", "provider", "model", "transcript", "known_media", "status"):
        value = getattr(patch, field)
        if value is not None:
            setattr(row, field, value)
    # Nullable, so presence decides: "series_id": null unbinds the show.
    if "series_id" in patch.model_fields_set:
        row.series_id = patch.series_id
    if patch.add_cost_usd is not None:
        # A SQL expression (SET cost_usd = cost_usd + :x), not a read-modify-write of
        # the value this request happened to load: concurrent increments both land.
        row.cost_usd = ChatThread.cost_usd + _usd(patch.add_cost_usd)
    await session.commit()
    await session.refresh(row)
    return row


async def delete_thread(session: AsyncSession, user_id: str, thread_id: str) -> bool:
    row = await _owned_thread(session, user_id, thread_id)
    if row is None:
        return False
    # Explicit rather than trusting ON DELETE CASCADE alone: sqlite test engines do not
    # enforce foreign keys, and the children must never outlive the thread anywhere.
    await session.execute(delete(ChatApproval).where(ChatApproval.thread_id == row.id))
    await session.execute(delete(ChatMessage).where(ChatMessage.thread_id == row.id))
    await session.execute(delete(ChatThread).where(ChatThread.id == row.id))
    await session.commit()
    return True


async def _touch_thread(session: AsyncSession, thread_id: str) -> None:
    await session.execute(
        update(ChatThread)
        .where(ChatThread.id == thread_id)
        .values(updated_at=_now())
        .execution_options(synchronize_session=False)
    )


# ── messages ─────────────────────────────────────────────────────────────────


async def _next_seq(session: AsyncSession, thread_id: str) -> int:
    top = (
        await session.execute(
            select(func.coalesce(func.max(ChatMessage.seq), 0)).where(ChatMessage.thread_id == thread_id)
        )
    ).scalar_one()
    return int(top) + 1


async def create_message(
    session: AsyncSession, user_id: str, thread_id: str, payload: ChatMessageIn
) -> ChatMessage | None:
    thread = await _owned_thread(session, user_id, thread_id)
    if thread is None:
        return None
    tid = thread.id
    message_id = payload.id or str(uuid.uuid4())

    async def already_stored() -> ChatMessage | None:
        # Idempotent on a caller-minted id: a retried POST returns the stored message.
        existing = await session.get(ChatMessage, message_id)
        if existing is None:
            return None
        if existing.thread_id == tid:
            return existing
        raise ChatError(409, "message_id_taken")

    for _ in range(_SEQ_ATTEMPTS):
        row = ChatMessage(
            id=message_id,
            thread_id=tid,
            seq=await _next_seq(session, tid),
            role=payload.role,
            kind=payload.kind,
            text=payload.text,
            data=dict(payload.data),
        )
        session.add(row)
        try:
            await _touch_thread(session, tid)
            await session.commit()
        except IntegrityError:
            await session.rollback()
            if (existing := await already_stored()) is not None:
                return existing  # two POSTs with the same minted id: the other won
            continue  # another message took this seq: recompute it
        await session.refresh(row)
        return row
    raise ChatError(409, "seq_conflict")


async def list_messages(
    session: AsyncSession, user_id: str, thread_id: str, *, after_seq: int = 0, limit: int = LIST_MESSAGES_DEFAULT
) -> list[ChatMessage] | None:
    thread = await _owned_thread(session, user_id, thread_id)
    if thread is None:
        return None
    query = (
        select(ChatMessage)
        .where(ChatMessage.thread_id == thread.id, ChatMessage.seq > after_seq)
        .order_by(ChatMessage.seq)
        .limit(max(1, min(limit, LIST_MESSAGES_MAX)))
    )
    return list((await session.execute(query)).scalars())


async def patch_message(
    session: AsyncSession, user_id: str, message_id: str, patch: ChatMessagePatch
) -> ChatMessage | None:
    mid = _uuid(message_id)
    if mid is None:
        return None
    row = (
        await session.execute(
            select(ChatMessage)
            .join(ChatThread, ChatThread.id == ChatMessage.thread_id)
            .where(ChatMessage.id == mid, ChatThread.user_id == user_id)
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    if patch.text is not None:
        row.text = patch.text
    if patch.data is not None:
        row.data = dict(patch.data)  # replaces; it is not merged
    await _touch_thread(session, row.thread_id)
    await session.commit()
    await session.refresh(row)
    return row


# ── approvals ────────────────────────────────────────────────────────────────


async def _owned_approval(session: AsyncSession, user_id: str, approval_id: str) -> ChatApproval | None:
    aid = _uuid(approval_id)
    if aid is None:
        return None
    return (
        await session.execute(
            select(ChatApproval)
            .join(ChatThread, ChatThread.id == ChatApproval.thread_id)
            .where(ChatApproval.id == aid, ChatThread.user_id == user_id)
        )
    ).scalar_one_or_none()


async def create_approval(
    session: AsyncSession, user_id: str, thread_id: str, payload: ChatApprovalIn
) -> ChatApproval | None:
    thread = await _owned_thread(session, user_id, thread_id)
    if thread is None:
        return None
    tid = thread.id  # read now: the rollback of a lost id race expires the thread
    row = ChatApproval(
        id=payload.id or str(uuid.uuid4()),
        thread_id=tid,
        message_id=payload.message_id,
        action=payload.action,
        subject=payload.subject,
        amount_usd=_usd(payload.amount_usd),
        summary=payload.summary,
        breakdown=payload.breakdown,
    )
    return await _insert_once(
        session, row, owned_by_caller=lambda existing: existing.thread_id == tid, taken="approval_id_taken"
    )


async def get_approval(session: AsyncSession, user_id: str, approval_id: str) -> ChatApproval | None:
    return await _owned_approval(session, user_id, approval_id)


async def _decide(
    session: AsyncSession, user_id: str, approval_id: str, **values: object
) -> ChatApproval | None:
    """Move a pending approval to the state in ``values``; any other state is refused.

    The status guard is part of the UPDATE, so two concurrent button presses cannot both win.
    """
    row = await _owned_approval(session, user_id, approval_id)
    if row is None:
        return None
    if row.status != "pending":
        raise ChatError(409, "approval_not_pending")
    try:
        result = await session.execute(
            update(ChatApproval)
            .where(ChatApproval.id == row.id, ChatApproval.status == "pending")
            .values(decided_at=_now(), **values)
            .execution_options(synchronize_session=False)
        )
    except IntegrityError:
        await session.rollback()
        raise ChatError(409, "token_taken")  # the hash already backs another approval here
    if result.rowcount != 1:
        await session.rollback()
        raise ChatError(409, "approval_not_pending")
    await session.commit()
    await session.refresh(row)
    return row


async def approve_approval(
    session: AsyncSession, user_id: str, approval_id: str, body: ChatApprovalApprove
) -> ChatApproval | None:
    return await _decide(
        session, user_id, approval_id,
        status="approved", token_sha256=body.token_sha256, expires_at=body.expires_at,
    )


async def decline_approval(session: AsyncSession, user_id: str, approval_id: str) -> ChatApproval | None:
    return await _decide(session, user_id, approval_id, status="declined")


async def _approval_by_token(
    session: AsyncSession, user_id: str, thread_id: str, token_sha256: str
) -> ChatApproval | None:
    """The approval a token was minted for. Only in the caller's own thread: another
    thread's (or another user's) token is simply not found."""
    tid = _uuid(thread_id)
    if tid is None or not token_sha256:
        return None
    return (
        await session.execute(
            select(ChatApproval)
            .join(ChatThread, ChatThread.id == ChatApproval.thread_id)
            .where(
                ChatApproval.thread_id == tid,
                ChatApproval.token_sha256 == token_sha256,
                ChatThread.user_id == user_id,
            )
            .with_for_update(of=ChatApproval)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


def _refusal(row: ChatApproval, body: ChatApprovalConsume) -> str | None:
    """Why this approval cannot pay for this call, or None when it can. The first mismatch wins."""
    if row.status != "approved":
        return row.status  # pending, declined, used or expired
    if row.expires_at is None or _aware(row.expires_at) <= _now():
        return "expired"
    if row.action != body.action:
        return "wrong_action"
    if row.subject != body.subject:
        return "wrong_subject"
    if _usd(row.amount_usd) + AMOUNT_TOLERANCE_USD < _usd(body.needed_usd):
        return "amount_exceeded"
    return None


async def consume_approval(session: AsyncSession, user_id: str, body: ChatApprovalConsume) -> ChatApproval:
    """Turn ``approved`` into ``used``, once, when the call matches what was approved.

    A refusal leaves the row exactly as it was (an ``amount_exceeded`` approval stays
    ``approved`` and usable for a smaller call). Everything runs in one transaction: the
    row is locked while it is checked, and the UPDATE repeats the status guard so a lost
    race can never spend an approval twice.
    """
    row = await _approval_by_token(session, user_id, body.thread_id, body.token_sha256)
    if row is None:
        raise ChatError(409, "unknown_token")
    if (reason := _refusal(row, body)) is not None:
        await session.rollback()
        raise ChatError(409, reason)
    approval_id = row.id  # read before any rollback expires the row
    result = await session.execute(
        update(ChatApproval)
        .where(ChatApproval.id == approval_id, ChatApproval.status == "approved")
        .values(status="used", used_at=_now())
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        await session.rollback()
        current = (
            await session.execute(select(ChatApproval.status).where(ChatApproval.id == approval_id))
        ).scalar_one_or_none()
        raise ChatError(409, current if current not in (None, "approved") else "used")
    await session.commit()
    await session.refresh(row)
    return row
