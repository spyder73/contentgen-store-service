from __future__ import annotations

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import PromptTemplate
from ..schemas import PromptTemplateIn, PromptTemplateOut

VALID_VISIBILITY = {"private", "global"}


async def list_prompts(session: AsyncSession, user_id: str | None = None, admin: bool = False) -> list[PromptTemplateOut]:
    stmt = select(PromptTemplate)
    if admin:
        pass
    elif user_id:
        stmt = stmt.where(
            or_(PromptTemplate.user_id == user_id, PromptTemplate.visibility == "global")
        )
    else:
        stmt = stmt.where(PromptTemplate.visibility == "global")
    stmt = stmt.order_by(PromptTemplate.name)
    result = await session.execute(stmt)
    return [PromptTemplateOut.from_orm_row(row) for row in result.scalars()]


async def get_prompt(session: AsyncSession, id: str, user_id: str | None = None) -> PromptTemplateOut | None:
    row = await session.get(PromptTemplate, id)
    if row is None:
        return None
    if row.user_id != user_id and row.visibility != "global":
        return None
    return PromptTemplateOut.from_orm_row(row)


async def upsert_prompt(
    session: AsyncSession,
    body: PromptTemplateIn,
    user_id: str | None = None,
    admin: bool = False,
) -> PromptTemplateOut:
    row = await session.get(PromptTemplate, body.id)
    if row is None:
        row = PromptTemplate(id=body.id)
        session.add(row)
    else:
        if not admin and row.user_id != user_id:
            return PromptTemplateOut.from_orm_row(row)
    row.name = body.name
    row.content = body.content
    row.metadata_ = body.metadata
    row.visibility = body.visibility if admin and body.visibility in VALID_VISIBILITY else "private"
    if admin:
        row.user_id = body.user_id
    elif row.user_id is None and user_id is not None:
        row.user_id = user_id
    await session.commit()
    await session.refresh(row)
    return PromptTemplateOut.from_orm_row(row)


async def seed_prompt(session: AsyncSession, body: PromptTemplateIn) -> PromptTemplateOut:
    """Seed a built-in prompt template if it is missing.

    Create-if-missing semantics only: when a row with this id already exists it
    is returned untouched. This protects user edits to built-in templates — the
    backend re-seeds its local-asset prompts on every startup, and re-seeding
    must never clobber a content change someone made through the normal upsert
    path. Seeded rows are global (no owner) so every service can resolve them.
    """
    row = await session.get(PromptTemplate, body.id)
    if row is not None:
        return PromptTemplateOut.from_orm_row(row)
    row = PromptTemplate(id=body.id)
    row.name = body.name
    row.content = body.content
    row.metadata_ = body.metadata
    row.visibility = body.visibility if body.visibility in VALID_VISIBILITY else "global"
    row.user_id = body.user_id
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return PromptTemplateOut.from_orm_row(row)


async def delete_prompt(session: AsyncSession, id: str, user_id: str | None = None, admin: bool = False) -> bool:
    row = await session.get(PromptTemplate, id)
    if row is None:
        return False
    if not admin and row.user_id != user_id:
        return False
    result = await session.execute(delete(PromptTemplate).where(PromptTemplate.id == id))
    await session.commit()
    return result.rowcount > 0
