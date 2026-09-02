from __future__ import annotations

"""Idea library rows — the unit the user browses, reruns and (via a clip)
rates.

List merging happens in Python rather than SQL on purpose: the library is
small (hundreds of rows), the score is an AVG that in normal single-shot use
is just the one clip's rating, and the note shown is the LATEST rating's note
(the freshest human verdict). Keeping the merge here keeps the SQL trivial
and the behavior unit-testable on sqlite.
"""

import uuid as uuidlib

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import ClipRating, Idea
from ..schemas import IdeaIn

# Fetch ceiling before Python-side min_score filtering; far above any real
# library today, revisit if a library ever outgrows it.
LIST_FETCH_CAP = 500


async def create_idea(session: AsyncSession, user_id: str, payload: IdeaIn) -> Idea:
    row = Idea(
        id=payload.id or str(uuidlib.uuid4()),
        user_id=user_id,
        seed=payload.seed,
        refined=None,
        template_id=payload.template_id,
        template_name=payload.template_name or "",
        params=dict(payload.params or {}),
        run_id=payload.run_id,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def get_idea(session: AsyncSession, idea_id: str, user_id: str) -> Idea | None:
    row = await session.get(Idea, idea_id)
    if row is None or row.user_id != user_id:
        return None
    return row


async def patch_refined(session: AsyncSession, idea_id: str, user_id: str, refined: str) -> Idea | None:
    row = await get_idea(session, idea_id, user_id)
    if row is None:
        return None
    row.refined = refined
    await session.commit()
    await session.refresh(row)
    return row


async def list_ideas(
    session: AsyncSession,
    user_id: str,
    template_id: str | None = None,
    run_id: str | None = None,
    min_score: float | None = None,
    limit: int = 50,
) -> list[dict]:
    query = select(Idea).where(Idea.user_id == user_id)
    if template_id:
        query = query.where(Idea.template_id == template_id)
    if run_id:
        query = query.where(Idea.run_id == run_id)
    query = query.order_by(Idea.created_at.desc()).limit(LIST_FETCH_CAP)
    ideas = (await session.execute(query)).scalars().all()
    if not ideas:
        return []

    ratings = (
        (
            await session.execute(
                select(ClipRating).where(ClipRating.idea_id.in_([i.id for i in ideas]))
            )
        )
        .scalars()
        .all()
    )
    by_idea: dict[str, list[ClipRating]] = {}
    for rating in ratings:
        by_idea.setdefault(rating.idea_id, []).append(rating)

    merged: list[dict] = []
    for idea in ideas:
        own = by_idea.get(idea.id, [])
        score = sum(r.score for r in own) / len(own) if own else None
        note = None
        if own:
            latest = max(own, key=lambda r: (r.updated_at or r.created_at, r.id))
            note = latest.note or None
        if min_score is not None and (score is None or score < min_score):
            continue
        merged.append(
            {
                "id": idea.id,
                "user_id": idea.user_id,
                "seed": idea.seed,
                "refined": idea.refined,
                "template_id": idea.template_id,
                "template_name": idea.template_name,
                "params": idea.params or {},
                "run_id": idea.run_id,
                "created_at": idea.created_at,
                "score": score,
                "note": note,
            }
        )
        if len(merged) >= limit:
            break
    return merged
