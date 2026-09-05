from __future__ import annotations

"""Human corrections on reviewer verdicts — exactly one ruling per
(user, verdict), upserted in place.

Re-judging a verdict (the human changes their mind) updates the existing
row rather than forking history, matching how ``clip_ratings`` upserts on
clip_id.
"""

import uuid as uuidlib
from datetime import datetime, timezone

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import ReviewCorrection
from ..schemas import ReviewCorrectionIn

LIST_LIMIT_DEFAULT = 50
LIST_LIMIT_MAX = 200


class ReviewCorrectionError(Exception):
    """Domain error carrying an HTTP status; routes translate it."""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


async def get_correction(session: AsyncSession, user_id: str, verdict_id: str) -> ReviewCorrection | None:
    query = select(ReviewCorrection).where(
        ReviewCorrection.user_id == user_id, ReviewCorrection.verdict_id == verdict_id
    )
    return (await session.execute(query)).scalars().first()


async def get_correction_by_id(session: AsyncSession, user_id: str, correction_id: str) -> ReviewCorrection | None:
    query = select(ReviewCorrection).where(
        ReviewCorrection.user_id == user_id, ReviewCorrection.id == correction_id
    )
    return (await session.execute(query)).scalars().first()


async def upsert_correction(
    session: AsyncSession, user_id: str, payload: ReviewCorrectionIn
) -> ReviewCorrection:
    row = await get_correction(session, user_id, payload.verdict_id)
    now = datetime.now(timezone.utc)
    is_insert = row is None

    def _apply(target: ReviewCorrection) -> None:
        target.label = payload.label
        target.reason = payload.reason or ""
        target.scope = payload.scope
        target.source = payload.source
        target.trace_id = payload.trace_id
        target.lesson = payload.lesson or ""
        target.updated_at = now

    if is_insert:
        row = ReviewCorrection(
            id=str(uuidlib.uuid4()),
            user_id=user_id,
            run_id=payload.run_id,
            verdict_id=payload.verdict_id,
            trace_id=payload.trace_id,
            template_id=payload.template_id,
            checkpoint_id=payload.checkpoint_id,
            tier=payload.tier or "",
            frame=payload.frame or "",
            label=payload.label,
            reason=payload.reason or "",
            scope=payload.scope,
            source=payload.source,
            lesson=payload.lesson or "",
            reinforces_id=payload.reinforces_id,
            created_at=now,
            updated_at=now,
        )
        session.add(row)
    else:
        _apply(row)

    # A brand-new correction can reinforce a prior one (the human re-affirms
    # a lesson already on file), bumping that row's weight once. Re-judging
    # an existing correction never re-triggers this -- the increment happens
    # exactly once, at the reinforcing row's creation.
    if is_insert and payload.reinforces_id:
        target = await get_correction_by_id(session, user_id, payload.reinforces_id)
        if target is None:
            await session.rollback()
            raise ReviewCorrectionError(404, "reinforced_correction_not_found")
        target.weight += 1

    try:
        await session.commit()
    except IntegrityError:
        # Read-then-insert loses the UNIQUE(user_id, verdict_id) race when
        # two upserts for one verdict overlap: roll our INSERT back and
        # update the row that won instead of 500-ing. The rollback also
        # discards any reinforcement weight bump attempted above — if this
        # insert carried reinforces_id, that reinforcement is silently
        # dropped on this race (the caller gets a 200, not an error; the
        # weight bump just never lands). Known, accepted gap.
        await session.rollback()
        winner = await get_correction(session, user_id, payload.verdict_id)
        if winner is None:
            raise
        row = winner
        _apply(row)
        await session.commit()
    await session.refresh(row)
    return row


async def list_corrections(
    session: AsyncSession,
    user_id: str,
    template_id: str | None = None,
    label: str | None = None,
    limit: int = LIST_LIMIT_DEFAULT,
) -> list[ReviewCorrection]:
    limit = max(1, min(limit, LIST_LIMIT_MAX))
    query = select(ReviewCorrection).where(ReviewCorrection.user_id == user_id)
    if template_id:
        # Rows for this template at any scope, plus all_pipelines rows
        # minted under any other template -- those apply everywhere.
        query = query.where(
            or_(
                ReviewCorrection.template_id == template_id,
                ReviewCorrection.scope == "all_pipelines",
            )
        )
    if label:
        query = query.where(ReviewCorrection.label == label)
    query = query.order_by(ReviewCorrection.created_at.desc()).limit(limit)
    return (await session.execute(query)).scalars().all()
