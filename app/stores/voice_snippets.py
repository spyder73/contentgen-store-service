from __future__ import annotations

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import VoiceSnippet
from ..schemas import PagedResponse, VoiceSnippetOut


async def list_voice_snippets(
    session: AsyncSession, character_id: str | None = None, page: int = 1, limit: int = 50
) -> PagedResponse:
    offset = (page - 1) * limit
    where = []
    if character_id:
        where.append(VoiceSnippet.character_id == character_id)
    count_q = select(func.count()).select_from(VoiceSnippet)
    data_q = select(VoiceSnippet).order_by(VoiceSnippet.created_at.desc())
    for clause in where:
        count_q = count_q.where(clause)
        data_q = data_q.where(clause)
    total = (await session.execute(count_q)).scalar_one()
    result = await session.execute(data_q.offset(offset).limit(limit))
    items = [VoiceSnippetOut.from_orm_row(row) for row in result.scalars()]
    return PagedResponse(items=items, total=total, page=page, limit=limit)


async def get_voice_snippet(session: AsyncSession, id: str) -> VoiceSnippetOut | None:
    row = await session.get(VoiceSnippet, id)
    if row is None:
        return None
    return VoiceSnippetOut.from_orm_row(row)


async def delete_voice_snippet(session: AsyncSession, id: str) -> bool:
    result = await session.execute(delete(VoiceSnippet).where(VoiceSnippet.id == id))
    await session.commit()
    return result.rowcount > 0
