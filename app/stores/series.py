from __future__ import annotations

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Series
from ..schemas import PagedResponse, SeriesIn, SeriesOut


async def list_series(
    session: AsyncSession, *, user_id: str, page: int = 1, limit: int = 50
) -> PagedResponse:
    if not user_id:
        raise ValueError("user_id is required for series listing")
    offset = (page - 1) * limit
    query = select(func.count()).select_from(Series).where(Series.user_id == user_id)
    data_query = select(Series).where(Series.user_id == user_id)
    count_result = await session.execute(query)
    total = count_result.scalar_one()
    result = await session.execute(
        data_query.order_by(Series.created_at.desc()).offset(offset).limit(limit)
    )
    items = [SeriesOut.from_orm_row(row) for row in result.scalars()]
    return PagedResponse(items=items, total=total, page=page, limit=limit)


async def get_series(
    session: AsyncSession, id: str, user_id: str | None = None
) -> SeriesOut | None:
    if not user_id:
        raise ValueError("get_series requires user_id")
    row = await session.get(Series, id)
    if row is None:
        return None
    # Legacy rows with no owner stay readable; owned rows are user-scoped.
    if row.user_id is not None and row.user_id != user_id:
        return None
    return SeriesOut.from_orm_row(row)


async def upsert_series(
    session: AsyncSession, body: SeriesIn, user_id: str | None = None
) -> SeriesOut:
    row = await session.get(Series, body.id)
    if row is None:
        row = Series(id=body.id)
        if user_id:
            row.user_id = user_id
        session.add(row)
    row.name = body.name
    row.description = body.description
    row.concept = body.concept
    row.metadata_ = body.metadata
    await session.commit()
    await session.refresh(row)
    return SeriesOut.from_orm_row(row)


async def delete_series(
    session: AsyncSession, id: str, user_id: str | None = None
) -> bool:
    if not user_id:
        raise ValueError("delete_series requires user_id")
    result = await session.execute(
        delete(Series).where(
            Series.id == id,
            (Series.user_id == user_id) | (Series.user_id.is_(None)),
        )
    )
    await session.commit()
    return result.rowcount > 0
