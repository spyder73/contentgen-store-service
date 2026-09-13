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
    # Narrow write: a v2 field the caller never mentioned keeps its stored
    # value. The live frontend still PUTs the v1 shape, and a blind overwrite
    # would silently wipe the show's binding, memories, wiring and parameters.
    # An explicitly sent [] / {} is present, and does clear.
    #
    # template_id is nullable, so presence — not value — decides: a body that
    # never mentions it keeps the binding, "template_id": null unbinds the show.
    if "template_id" in body.model_fields_set:
        row.template_id = body.template_id
    if body.memories is not None:
        row.memories = body.memories
    if body.slot_map is not None:
        row.slot_map = body.slot_map
    if body.parameters is not None:
        row.parameters = body.parameters
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
