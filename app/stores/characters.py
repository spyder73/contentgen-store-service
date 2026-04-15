from __future__ import annotations

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Character
from ..schemas import CharacterIn, CharacterOut, PagedResponse


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


async def get_character(session: AsyncSession, id: str) -> CharacterOut | None:
    row = await session.get(Character, id)
    if row is None:
        return None
    return CharacterOut.from_orm_row(row)


async def upsert_character(session: AsyncSession, body: CharacterIn) -> CharacterOut:
    row = await session.get(Character, body.id)
    if row is None:
        row = Character(id=body.id)
        session.add(row)
    row.series_id = body.series_id
    row.name = body.name
    row.description = body.description
    row.voice = body.voice
    row.reference_image_media_id = body.reference_image_media_id
    row.metadata_ = body.metadata
    await session.commit()
    await session.refresh(row)
    return CharacterOut.from_orm_row(row)


async def delete_character(session: AsyncSession, id: str) -> bool:
    result = await session.execute(delete(Character).where(Character.id == id))
    await session.commit()
    return result.rowcount > 0
