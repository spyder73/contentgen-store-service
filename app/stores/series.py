from __future__ import annotations

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Series
from ..schemas import PagedResponse, SeriesIn, SeriesOut


async def list_series(
    session: AsyncSession, page: int = 1, limit: int = 50
) -> PagedResponse:
    offset = (page - 1) * limit
    count_result = await session.execute(select(func.count()).select_from(Series))
    total = count_result.scalar_one()
    result = await session.execute(
        select(Series).order_by(Series.created_at.desc()).offset(offset).limit(limit)
    )
    items = [SeriesOut.from_orm_row(row) for row in result.scalars()]
    return PagedResponse(items=items, total=total, page=page, limit=limit)


async def get_series(session: AsyncSession, id: str) -> SeriesOut | None:
    row = await session.get(Series, id)
    if row is None:
        return None
    return SeriesOut.from_orm_row(row)


async def upsert_series(session: AsyncSession, body: SeriesIn) -> SeriesOut:
    row = await session.get(Series, body.id)
    if row is None:
        row = Series(id=body.id)
        session.add(row)
    row.name = body.name
    row.description = body.description
    row.concept = body.concept
    row.metadata_ = body.metadata
    await session.commit()
    await session.refresh(row)
    return SeriesOut.from_orm_row(row)


async def delete_series(session: AsyncSession, id: str) -> bool:
    result = await session.execute(delete(Series).where(Series.id == id))
    await session.commit()
    return result.rowcount > 0
