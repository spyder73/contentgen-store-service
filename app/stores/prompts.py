from __future__ import annotations

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import PromptTemplate
from ..schemas import PromptTemplateIn, PromptTemplateOut


async def list_prompts(session: AsyncSession, user_id: str | None = None) -> list[PromptTemplateOut]:
    stmt = select(PromptTemplate)
    if user_id:
        stmt = stmt.where(
            or_(PromptTemplate.user_id == user_id, PromptTemplate.user_id.is_(None))
        )
    else:
        stmt = stmt.where(PromptTemplate.user_id.is_(None))
    result = await session.execute(stmt)
    return [PromptTemplateOut.from_orm_row(row) for row in result.scalars()]


async def get_prompt(session: AsyncSession, id: str, user_id: str | None = None) -> PromptTemplateOut | None:
    row = await session.get(PromptTemplate, id)
    if row is None:
        return None
    if row.user_id is not None and (user_id is None or row.user_id != user_id):
        return None
    return PromptTemplateOut.from_orm_row(row)


async def upsert_prompt(session: AsyncSession, body: PromptTemplateIn, user_id: str | None = None) -> PromptTemplateOut:
    row = await session.get(PromptTemplate, body.id)
    if row is None:
        row = PromptTemplate(id=body.id)
        session.add(row)
    else:
        if row.user_id is not None and (user_id is None or row.user_id != user_id):
            return PromptTemplateOut.from_orm_row(row)
    row.name = body.name
    row.content = body.content
    row.metadata_ = body.metadata
    if row.user_id is None and user_id is not None:
        row.user_id = user_id
    await session.commit()
    await session.refresh(row)
    return PromptTemplateOut.from_orm_row(row)


async def delete_prompt(session: AsyncSession, id: str, user_id: str | None = None) -> bool:
    row = await session.get(PromptTemplate, id)
    if row is None:
        return False
    if row.user_id is not None and (user_id is None or row.user_id != user_id):
        return False
    result = await session.execute(delete(PromptTemplate).where(PromptTemplate.id == id))
    await session.commit()
    return result.rowcount > 0
