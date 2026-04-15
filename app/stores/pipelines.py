from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import PipelineTemplate
from ..schemas import PipelineTemplateIn, PipelineTemplateOut


async def list_pipelines(session: AsyncSession) -> list[PipelineTemplateOut]:
    result = await session.execute(select(PipelineTemplate))
    return [PipelineTemplateOut.model_validate(row) for row in result.scalars()]


async def get_pipeline(session: AsyncSession, id: str) -> PipelineTemplateOut | None:
    row = await session.get(PipelineTemplate, id)
    if row is None:
        return None
    return PipelineTemplateOut.model_validate(row)


async def upsert_pipeline(session: AsyncSession, body: PipelineTemplateIn) -> PipelineTemplateOut:
    row = await session.get(PipelineTemplate, body.id)
    if row is None:
        row = PipelineTemplate(id=body.id)
        session.add(row)
    row.name = body.name
    row.data = body.data
    row.version = body.version
    await session.commit()
    await session.refresh(row)
    return PipelineTemplateOut.model_validate(row)


async def delete_pipeline(session: AsyncSession, id: str) -> bool:
    result = await session.execute(delete(PipelineTemplate).where(PipelineTemplate.id == id))
    await session.commit()
    return result.rowcount > 0
