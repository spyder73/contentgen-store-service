from __future__ import annotations

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import PipelineTemplate
from ..schemas import PipelineTemplateIn, PipelineTemplateOut


async def list_pipelines(session: AsyncSession, user_id: str | None = None) -> list[PipelineTemplateOut]:
    stmt = select(PipelineTemplate)
    if user_id:
        stmt = stmt.where(
            or_(PipelineTemplate.user_id == user_id, PipelineTemplate.user_id.is_(None))
        )
    else:
        stmt = stmt.where(PipelineTemplate.user_id.is_(None))
    result = await session.execute(stmt)
    return [PipelineTemplateOut.model_validate(row) for row in result.scalars()]


async def get_pipeline(session: AsyncSession, id: str, user_id: str | None = None) -> PipelineTemplateOut | None:
    row = await session.get(PipelineTemplate, id)
    if row is None:
        return None
    # If the row has a user_id, only return it to the matching user.
    if row.user_id is not None and (user_id is None or row.user_id != user_id):
        return None
    return PipelineTemplateOut.model_validate(row)


async def upsert_pipeline(session: AsyncSession, body: PipelineTemplateIn, user_id: str |
 None = None) -> PipelineTemplateOut:
    row = await session.get(PipelineTemplate, body.id)
    if row is None:
        row = PipelineTemplate(id=body.id)
        session.add(row)
    else:
        # Ownership check — only the owner can update a non-global template.
        if row.user_id is not None and (user_id is None or row.user_id != user_id):
            return PipelineTemplateOut.model_validate(row)  # silently drop or raise in endpoint
    row.name = body.name
    row.data = body.data
    row.version = body.version
    # Set user_id on creation; keep existing on update.
    if row.user_id is None and user_id is not None:
        row.user_id = user_id
    await session.commit()
    await session.refresh(row)
    return PipelineTemplateOut.model_validate(row)


async def delete_pipeline(session: AsyncSession, id: str, user_id: str | None = None) -> bool:
    row = await session.get(PipelineTemplate, id)
    if row is None:
        return False
    # Global templates cannot be deleted by non-owners.
    if row.user_id is not None and (user_id is None or row.user_id != user_id):
        return False
    result = await session.execute(delete(PipelineTemplate).where(PipelineTemplate.id == id))
    await session.commit()
    return result.rowcount > 0
