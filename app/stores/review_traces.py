from __future__ import annotations

"""Reviewer trace log — an append-only record of every check-tier call the
reviewer made, one row per attempt.

``id`` is minted by the caller (the Go backend) before the call, so a
retried POST is idempotent: it must return the row already stored rather
than fail on the primary key or fork the log.
"""

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import ReviewTrace
from ..schemas import ReviewTraceIn

LIST_LIMIT_DEFAULT = 200
LIST_LIMIT_MAX = 200


class ReviewTraceError(Exception):
    """Domain error carrying an HTTP status; routes translate it."""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def _row_from_payload(user_id: str, payload: ReviewTraceIn) -> ReviewTrace:
    return ReviewTrace(
        id=payload.id,
        user_id=user_id,
        run_id=payload.run_id,
        template_id=payload.template_id or "",
        checkpoint_id=payload.checkpoint_id,
        checkpoint_index=payload.checkpoint_index,
        verdict_id=payload.verdict_id,
        attempt=payload.attempt,
        tier=payload.tier,
        frame=payload.frame or "",
        candidate_id=payload.candidate_id or "",
        provider=payload.provider or "",
        model=payload.model or "",
        system_prompt=payload.system_prompt or "",
        raw_output=payload.raw_output or "",
        images=list(payload.images or []),
        prompt_chars=payload.prompt_chars,
        prompt_hash=payload.prompt_hash or "",
        latency_ms=payload.latency_ms,
        outcome=payload.outcome or "",
    )


async def create_trace(session: AsyncSession, user_id: str, payload: ReviewTraceIn) -> ReviewTrace:
    # Idempotent on id: a retried POST (network hiccup, at-least-once
    # delivery) must return the row already stored rather than 409/500. The
    # PK is id alone, so a collision with a DIFFERENT user's trace is a
    # conflict, never a read of that row: both the pre-check below and the
    # post-IntegrityError re-read are scoped by user_id.
    existing = await session.get(ReviewTrace, payload.id)
    if existing is not None:
        if existing.user_id == user_id:
            return existing
        raise ReviewTraceError(409, "trace_id_taken")

    row = _row_from_payload(user_id, payload)
    session.add(row)
    try:
        await session.commit()
    except IntegrityError:
        # Two concurrent POSTs with the same caller-minted id: the loser's
        # INSERT lost the primary-key race, so roll back and read the
        # winner instead of failing the request -- but only if the winner
        # is OUR row (the same-user retry case). A winner owned by a
        # different user is a real conflict, not a row to hand back.
        await session.rollback()
        winner = (
            await session.execute(
                select(ReviewTrace).where(
                    ReviewTrace.id == payload.id, ReviewTrace.user_id == user_id
                )
            )
        ).scalars().first()
        if winner is not None:
            return winner
        other = await session.get(ReviewTrace, payload.id)
        if other is not None:
            raise ReviewTraceError(409, "trace_id_taken")
        raise
    await session.refresh(row)
    return row


async def list_traces(
    session: AsyncSession, user_id: str, run_id: str, limit: int = LIST_LIMIT_DEFAULT
) -> list[ReviewTrace]:
    limit = max(1, min(limit, LIST_LIMIT_MAX))
    query = (
        select(ReviewTrace)
        .where(ReviewTrace.user_id == user_id, ReviewTrace.run_id == run_id)
        .order_by(ReviewTrace.created_at.asc())
        .limit(limit)
    )
    return (await session.execute(query)).scalars().all()
