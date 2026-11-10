from __future__ import annotations

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Series
from ..schemas import PagedResponse, SeriesIn, SeriesOut


class SeriesError(Exception):
    """Domain error for the series and cast writes, carrying an HTTP status;
    the routes translate it (see GeneratorProfileError)."""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


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


async def series_is_callers(session: AsyncSession, series_id: str, user_id: str | None) -> bool:
    """The series exists and is the caller's (a legacy row with no owner is
    anyone's, as it is for reads). A caller with no user owns none: it is not
    let into a legacy series either."""
    if not user_id:
        return False
    row = await session.get(Series, series_id)
    return row is not None and (row.user_id is None or row.user_id == user_id)


async def upsert_series(
    session: AsyncSession, body: SeriesIn, user_id: str | None = None
) -> SeriesOut:
    if not user_id:
        # A new row would be minted with no owner, which every user may read
        # and write: the route answers 401, and this never fails open.
        raise ValueError("upsert_series requires user_id")
    row = await session.get(Series, body.id)
    # Another user's series is not found, as a read of it is, and nothing is
    # written: the id is the caller's choice (the backend's create route takes
    # it from the request), so an upsert must not merge into a row it does not
    # own. Legacy rows with no owner stay writable, as they stay readable; a
    # write does not claim one.
    if row is not None and row.user_id is not None and row.user_id != user_id:
        raise SeriesError(404, "not_found")
    if row is None:
        if body.name is None:
            raise SeriesError(422, "name is required for a new series")
        row = Series(id=body.id, user_id=user_id)
        session.add(row)
    # Narrow write: a field the caller never mentioned keeps its stored value,
    # so a caller can send just the keys it changes. (A new row takes the column
    # defaults, "" and {}.) The live frontend still PUTs the v1 shape, and a
    # blind overwrite would silently wipe the show's description, binding,
    # memories, wiring and parameters. An explicitly sent "" / [] / {} is
    # present, and does clear.
    #
    # A null on the NOT NULL columns counts as "not mentioned". template_id is
    # nullable, so presence — not value — decides: a body that never mentions it
    # keeps the binding, "template_id": null unbinds the show.
    if body.name is not None:
        row.name = body.name
    if body.description is not None:
        row.description = body.description
    if body.concept is not None:
        row.concept = body.concept
    if "template_id" in body.model_fields_set:
        row.template_id = body.template_id
    if body.memories is not None:
        row.memories = body.memories
    if body.slot_map is not None:
        row.slot_map = body.slot_map
    if body.parameters is not None:
        row.parameters = body.parameters
    if body.format_brief is not None:
        row.format_brief = body.format_brief
    if body.metadata is not None:
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
