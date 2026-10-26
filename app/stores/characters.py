from __future__ import annotations

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Character, Series
from ..schemas import CharacterIn, CharacterOut, PagedResponse
from .series import SeriesError


def _owned_series_ids(user_id: str):
    """Subquery of the series ids the user owns (legacy rows with no owner
    included). Characters have no user_id, so ownership is scoped through the
    parent series."""
    return select(Series.id).where(
        (Series.user_id == user_id) | (Series.user_id.is_(None))
    )


async def list_characters(
    session: AsyncSession, series_id: str | None = None, page: int = 1, limit: int = 50
) -> PagedResponse:
    offset = (page - 1) * limit
    where = []
    if series_id:
        where.append(Character.series_id == series_id)
    count_q = select(func.count()).select_from(Character)
    data_q = select(Character).order_by(Character.created_at.desc())
    for clause in where:
        count_q = count_q.where(clause)
        data_q = data_q.where(clause)
    total = (await session.execute(count_q)).scalar_one()
    result = await session.execute(data_q.offset(offset).limit(limit))
    items = [CharacterOut.from_orm_row(row) for row in result.scalars()]
    return PagedResponse(items=items, total=total, page=page, limit=limit)


async def get_character(
    session: AsyncSession, id: str, user_id: str | None = None
) -> CharacterOut | None:
    if not user_id:
        raise ValueError("get_character requires user_id")
    stmt = (
        select(Character)
        .where(Character.id == id, Character.series_id.in_(_owned_series_ids(user_id)))
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        return None
    return CharacterOut.from_orm_row(row)


async def _series_is_callers(session: AsyncSession, series_id: str, user_id: str | None) -> bool:
    """The series exists and is the caller's (a legacy row with no owner is
    anyone's, as it is for reads)."""
    series = await session.get(Series, series_id)
    return series is not None and (series.user_id is None or series.user_id == user_id)


async def upsert_character(
    session: AsyncSession, body: CharacterIn, user_id: str | None = None
) -> CharacterOut:
    row = await session.get(Character, body.id)
    # A character is the caller's through its series: the series it is in and
    # the one it is put in must both be theirs. Anything else is not found, as
    # a read of it is, and nothing is written — the id is the caller's choice,
    # so an upsert must not merge into another user's cast row, nor move one
    # into another user's series. A caller with no user owns no series.
    if row is not None and not await _series_is_callers(session, row.series_id, user_id):
        raise SeriesError(404, "not_found")
    if not await _series_is_callers(session, body.series_id, user_id):
        raise SeriesError(404, "not_found")
    if row is None:
        if body.name is None:
            raise SeriesError(422, "name is required for a new character")
        row = Character(id=body.id)
        session.add(row)
    row.series_id = body.series_id
    # Narrow write — see upsert_series: a field the caller never mentioned keeps
    # its stored value (a new row takes the column defaults), and a null on a
    # NOT NULL column counts as "not mentioned". The nullable ids go by
    # presence: absent keeps the link, "voice_media_id": null (likewise the
    # reference image and generator profile) unlinks it.
    if body.name is not None:
        row.name = body.name
    if body.description is not None:
        row.description = body.description
    if body.voice is not None:
        row.voice = body.voice
    if body.kind is not None:
        row.kind = body.kind
    if body.anchors is not None:
        row.anchors = body.anchors
    if "voice_media_id" in body.model_fields_set:
        row.voice_media_id = body.voice_media_id
    if "reference_image_media_id" in body.model_fields_set:
        row.reference_image_media_id = body.reference_image_media_id
    if "generator_profile_id" in body.model_fields_set:
        row.generator_profile_id = body.generator_profile_id
    if body.metadata is not None:
        row.metadata_ = body.metadata
    await session.commit()
    await session.refresh(row)
    return CharacterOut.from_orm_row(row)


async def delete_character(
    session: AsyncSession, id: str, user_id: str | None = None
) -> bool:
    if not user_id:
        raise ValueError("delete_character requires user_id")
    result = await session.execute(
        delete(Character).where(
            Character.id == id,
            Character.series_id.in_(_owned_series_ids(user_id)),
        )
    )
    await session.commit()
    return result.rowcount > 0
