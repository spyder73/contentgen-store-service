from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import PromptTemplate
from ..schemas import PromptTemplateIn, PromptTemplateOut


async def list_prompts(session: AsyncSession) -> list[PromptTemplateOut]:
    result = await session.execute(select(PromptTemplate))
    return [PromptTemplateOut.from_orm_row(row) for row in result.scalars()]


async def get_prompt(session: AsyncSession, id: str) -> PromptTemplateOut | None:
    row = await session.get(PromptTemplate, id)
    if row is None:
        return None
    return PromptTemplateOut.from_orm_row(row)


async def upsert_prompt(session: AsyncSession, body: PromptTemplateIn) -> PromptTemplateOut:
    row = await session.get(PromptTemplate, body.id)
    if row is None:
        row = PromptTemplate(id=body.id)
        session.add(row)
    row.name = body.name
    row.content = body.content
    row.metadata_ = body.metadata
    await session.commit()
    await session.refresh(row)
    return PromptTemplateOut.from_orm_row(row)


async def delete_prompt(session: AsyncSession, id: str) -> bool:
    result = await session.execute(delete(PromptTemplate).where(PromptTemplate.id == id))
    await session.commit()
    return result.rowcount > 0
