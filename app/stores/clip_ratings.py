from __future__ import annotations

"""Clip ratings — exactly one verdict per clip, upserted in place.

``idea_id`` is only ever OVERWRITTEN with a non-null value: the caller
re-resolves the clip→idea link on every upsert, and a resolution that failed
this time (None) must not erase a link that succeeded last time.
"""

import uuid as uuidlib
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import ClipRating
from ..schemas import ClipRatingIn


async def get_rating(session: AsyncSession, user_id: str, clip_id: str) -> ClipRating | None:
    query = select(ClipRating).where(
        ClipRating.user_id == user_id, ClipRating.clip_id == clip_id
    )
    return (await session.execute(query)).scalars().first()


async def upsert_rating(session: AsyncSession, user_id: str, payload: ClipRatingIn) -> ClipRating:
    row = await get_rating(session, user_id, payload.clip_id)
    # Client-side microsecond timestamps: the DB server_default is
    # second-granular on sqlite, and "latest note wins" in the library list
    # needs a total order even for ratings created in the same second.
    now = datetime.now(timezone.utc)
    if row is None:
        row = ClipRating(
            id=str(uuidlib.uuid4()),
            user_id=user_id,
            clip_id=payload.clip_id,
            idea_id=payload.idea_id,
            score=payload.score,
            note=payload.note or "",
            created_at=now,
            updated_at=now,
        )
        session.add(row)
    else:
        row.score = payload.score
        row.note = payload.note or ""
        row.updated_at = now
        if payload.idea_id:
            row.idea_id = payload.idea_id
    await session.commit()
    await session.refresh(row)
    return row
