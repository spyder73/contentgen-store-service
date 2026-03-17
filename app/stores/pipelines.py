from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
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
    stmt = (
        insert(PipelineTemplate)
        .values(id=body.id, name=body.name, data=body.data, version=body.version)
        .on_conflict_do_update(
            index_elements=["id"],
            set_={"name": body.name, "data": body.data, "version": body.version},
        )
        .returning(PipelineTemplate)
    )
    result = await session.execute(stmt)
    await session.commit()
    row = result.scalars().one()
    return PipelineTemplateOut.model_validate(row)


async def delete_pipeline(session: AsyncSession, id: str) -> bool:
    result = await session.execute(
        delete(PipelineTemplate).where(PipelineTemplate.id == id)
    )
    await session.commit()
    return result.rowcount > 0
