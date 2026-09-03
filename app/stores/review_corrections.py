from __future__ import annotations

"""Human corrections on reviewer verdicts — exactly one ruling per
(user, verdict), upserted in place.

Re-judging a verdict (the human changes their mind) updates the existing
row rather than forking history, matching how ``clip_ratings`` upserts on
clip_id.
"""

import uuid as uuidlib
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import ReviewCorrection
from ..schemas import ReviewCorrectionIn

LIST_LIMIT_DEFAULT = 50
LIST_LIMIT_MAX = 200


async def get_correction(session: AsyncSession, user_id: str, verdict_id: str) -> ReviewCorrection | None:
    query = select(ReviewCorrection).where(
        ReviewCorrection.user_id == user_id, ReviewCorrection.verdict_id == verdict_id
    )
    return (await session.execute(query)).scalars().first()


async def upsert_correction(
    session: AsyncSession, user_id: str, payload: ReviewCorrectionIn
) -> ReviewCorrection:
    row = await get_correction(session, user_id, payload.verdict_id)
    now = datetime.now(timezone.utc)

    def _apply(target: ReviewCorrection) -> None:
        target.label = payload.label
        target.reason = payload.reason or ""
        target.scope = payload.scope
        target.source = payload.source
        target.trace_id = payload.trace_id
        target.updated_at = now

    if row is None:
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
            created_at=now,
            updated_at=now,
        )
        session.add(row)
    else:
        _apply(row)
    try:
        await session.commit()
    except IntegrityError:
        # Read-then-insert loses the UNIQUE(user_id, verdict_id) race when
        # two upserts for one verdict overlap: roll our INSERT back and
        # update the row that won instead of 500-ing.
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
        query = query.where(ReviewCorrection.template_id == template_id)
    if label:
        query = query.where(ReviewCorrection.label == label)
    query = query.order_by(ReviewCorrection.created_at.desc()).limit(limit)
    return (await session.execute(query)).scalars().all()
