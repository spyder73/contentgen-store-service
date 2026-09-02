from __future__ import annotations

"""Clip ratings — exactly one verdict per clip, upserted in place.

``idea_id`` is only ever OVERWRITTEN with a non-null value: the caller
re-resolves the clip→idea link on every upsert, and a resolution that failed
this time (None) must not erase a link that succeeded last time.
"""

import uuid as uuidlib
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import ClipPrompt, ClipRating, Idea
from ..schemas import ClipRatingIn


class ClipRatingError(Exception):
    """Domain error carrying an HTTP status; routes translate it."""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


async def get_rating(session: AsyncSession, user_id: str, clip_id: str) -> ClipRating | None:
    query = select(ClipRating).where(
        ClipRating.user_id == user_id, ClipRating.clip_id == clip_id
    )
    return (await session.execute(query)).scalars().first()


async def upsert_rating(session: AsyncSession, user_id: str, payload: ClipRatingIn) -> ClipRating:
    # The store defends itself even though the Go backend owner-checks first:
    # clip_id is UNIQUE across all users, so a rating forged against someone
    # else's clip would permanently block the real owner from rating it.
    # An EMPTY owner is accessible, matching the backend's convention: 0007
    # added clip_prompts.user_id without a backfill, so pre-multi-tenancy
    # clips have NULL and only a DIFFERENT non-empty owner is refused.
    clip = await session.get(ClipPrompt, payload.clip_id)
    owner = ((clip.user_id if clip is not None else "") or "").strip()
    if clip is None or (owner and owner != user_id):
        raise ClipRatingError(404, "clip_not_found")

    # A link may only point at the caller's own idea; anything else is stored
    # as unlinked rather than rejected (the caller re-resolves on re-rate).
    idea_id = payload.idea_id
    if idea_id:
        idea = await session.get(Idea, idea_id)
        if idea is None or idea.user_id != user_id:
            idea_id = None

    row = await get_rating(session, user_id, payload.clip_id)
    # Client-side microsecond timestamps: the DB server_default is
    # second-granular on sqlite, and "latest note wins" in the library list
    # needs a total order even for ratings created in the same second.
    now = datetime.now(timezone.utc)

    def _apply(target: ClipRating) -> None:
        target.score = payload.score
        target.note = payload.note or ""
        target.updated_at = now
        if idea_id:
            target.idea_id = idea_id

    if row is None:
        row = ClipRating(
            id=str(uuidlib.uuid4()),
            user_id=user_id,
            clip_id=payload.clip_id,
            idea_id=idea_id,
            score=payload.score,
            note=payload.note or "",
            created_at=now,
            updated_at=now,
        )
        session.add(row)
    else:
        _apply(row)
    try:
        await session.commit()
    except IntegrityError:
        # Read-then-insert loses the UNIQUE(clip_id) race when two upserts for
        # one clip overlap (star click + note blur): roll our INSERT back and
        # update the row that won instead of 500-ing. A missing winner means
        # the conflict was not ours to resolve, so it propagates.
        await session.rollback()
        winner = await get_rating(session, user_id, payload.clip_id)
        if winner is None:
            raise
        row = winner
        _apply(row)
        await session.commit()
    await session.refresh(row)
    return row
