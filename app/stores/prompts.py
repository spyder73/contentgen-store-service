from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
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
    stmt = (
        insert(PromptTemplate)
        .values(
            id=body.id,
            name=body.name,
            content=body.content,
            metadata=body.metadata,
        )
        .on_conflict_do_update(
            index_elements=["id"],
            set_={"name": body.name, "content": body.content, "metadata": body.metadata},
        )
        .returning(PromptTemplate)
    )
    result = await session.execute(stmt)
    await session.commit()
    row = result.scalars().one()
    return PromptTemplateOut.from_orm_row(row)


async def delete_prompt(session: AsyncSession, id: str) -> bool:
    result = await session.execute(
        delete(PromptTemplate).where(PromptTemplate.id == id)
    )
    await session.commit()
    return result.rowcount > 0
