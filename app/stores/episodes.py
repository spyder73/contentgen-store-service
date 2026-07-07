from __future__ import annotations

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Episode, Series
from ..schemas import EpisodeIn, EpisodeOut, PagedResponse


def _owned_series_ids(user_id: str):
    """Subquery of the series ids the user owns (legacy rows with no owner
    included). Episodes have no user_id, so ownership is scoped through the
    parent series."""
    return select(Series.id).where(
        (Series.user_id == user_id) | (Series.user_id.is_(None))
    )


async def list_episodes(
    session: AsyncSession, series_id: str | None = None, page: int = 1, limit: int = 50
) -> PagedResponse:
    offset = (page - 1) * limit
    where = []
    if series_id:
        where.append(Episode.series_id == series_id)
    count_q = select(func.count()).select_from(Episode)
    data_q = select(Episode).order_by(Episode.episode_number.asc())
    for clause in where:
        count_q = count_q.where(clause)
        data_q = data_q.where(clause)
    total = (await session.execute(count_q)).scalar_one()
    result = await session.execute(data_q.offset(offset).limit(limit))
    items = [EpisodeOut.from_orm_row(row) for row in result.scalars()]
    return PagedResponse(items=items, total=total, page=page, limit=limit)


async def get_episode(
    session: AsyncSession, id: str, user_id: str | None = None
) -> EpisodeOut | None:
    if not user_id:
        raise ValueError("get_episode requires user_id")
    stmt = (
        select(Episode)
        .where(Episode.id == id, Episode.series_id.in_(_owned_series_ids(user_id)))
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        return None
    return EpisodeOut.from_orm_row(row)


async def upsert_episode(session: AsyncSession, body: EpisodeIn) -> EpisodeOut:
    row = await session.get(Episode, body.id)
    if row is None:
        row = Episode(id=body.id)
        session.add(row)
    row.series_id = body.series_id
    row.episode_number = body.episode_number
    row.title = body.title
    row.synopsis = body.synopsis
    row.prev_episode_summary = body.prev_episode_summary
    row.metadata_ = body.metadata
    await session.commit()
    await session.refresh(row)
    return EpisodeOut.from_orm_row(row)


async def delete_episode(
    session: AsyncSession, id: str, user_id: str | None = None
) -> bool:
    if not user_id:
        raise ValueError("delete_episode requires user_id")
    result = await session.execute(
        delete(Episode).where(
            Episode.id == id,
            Episode.series_id.in_(_owned_series_ids(user_id)),
        )
    )
    await session.commit()
    return result.rowcount > 0
