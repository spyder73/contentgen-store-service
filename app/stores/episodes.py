from __future__ import annotations

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Episode, Series
from ..schemas import EpisodeIn, EpisodeOut, EpisodePatch, PagedResponse


# Ledger references that may legitimately be cleared: an explicit null unlinks,
# an absent key leaves the stored value alone.
_NULLABLE_LEDGER_FIELDS = ("run_id", "idea_id", "clip_id", "last_frame_media_id")


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
    # Narrow write — see upsert_series. Renaming an episode from the old UI
    # must not flip a running episode back to 'draft'.
    if body.status is not None:
        row.status = body.status
    if body.storyline is not None:
        row.storyline = body.storyline
    # The nullable ledger references go by presence, so a run that failed to
    # produce a clip can be unlinked with an explicit null instead of needing a
    # second route.
    for field in _NULLABLE_LEDGER_FIELDS:
        if field in body.model_fields_set:
            setattr(row, field, getattr(body, field))
    row.metadata_ = body.metadata
    await session.commit()
    await session.refresh(row)
    return EpisodeOut.from_orm_row(row)


# Patch field name -> ORM attribute, for the one field whose column name is
# taken by SQLAlchemy's own ``metadata``.
_PATCH_ATTRIBUTES = {"metadata": "metadata_"}


async def patch_episode(
    session: AsyncSession,
    id: str,
    body: EpisodePatch,
    user_id: str | None = None,
) -> EpisodeOut | None:
    """Write only the keys the caller actually sent. The recorder knows one
    fact at a time ({"status": "complete"}) and must not have to restate — or
    guess — the rest of the row. Owner-scoped: an episode outside the caller's
    series reads as missing."""
    if not user_id:
        raise ValueError("patch_episode requires user_id")
    stmt = select(Episode).where(
        Episode.id == id, Episode.series_id.in_(_owned_series_ids(user_id))
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        return None
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(row, _PATCH_ATTRIBUTES.get(field, field), value)
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
