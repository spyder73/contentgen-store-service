from __future__ import annotations

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Character, Series, VoiceSnippet
from ..schemas import PagedResponse, VoiceSnippetOut


def _owned_character_ids(user_id: str):
    """Subquery of the character ids the user owns (legacy series with no owner
    included). Voice snippets have no user_id, so ownership is scoped through the
    owning character's parent series."""
    return (
        select(Character.id)
        .join(Series, Character.series_id == Series.id)
        .where((Series.user_id == user_id) | (Series.user_id.is_(None)))
    )


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


async def get_voice_snippet(
    session: AsyncSession, id: str, user_id: str | None = None
) -> VoiceSnippetOut | None:
    if not user_id:
        raise ValueError("get_voice_snippet requires user_id")
    stmt = select(VoiceSnippet).where(
        VoiceSnippet.id == id,
        VoiceSnippet.character_id.in_(_owned_character_ids(user_id)),
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        return None
    return VoiceSnippetOut.from_orm_row(row)


async def delete_voice_snippet(
    session: AsyncSession, id: str, user_id: str | None = None
) -> bool:
    if not user_id:
        raise ValueError("delete_voice_snippet requires user_id")
    result = await session.execute(
        delete(VoiceSnippet).where(
            VoiceSnippet.id == id,
            VoiceSnippet.character_id.in_(_owned_character_ids(user_id)),
        )
    )
    await session.commit()
    return result.rowcount > 0
