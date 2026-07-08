from __future__ import annotations

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import DatasetTemplate
from ..schemas import (
    DatasetTemplateCreate,
    DatasetTemplateOut,
    DatasetTemplateUpdate,
)


def _out(row: DatasetTemplate) -> DatasetTemplateOut:
    return DatasetTemplateOut.model_validate(row)


async def list_templates(
    session: AsyncSession,
    *,
    user_id: str | None = None,
    include_system: bool = True,
) -> list[DatasetTemplateOut]:
    stmt = select(DatasetTemplate)
    conditions = []
    if include_system:
        conditions.append(DatasetTemplate.user_id.is_(None))
    if user_id:
        conditions.append(DatasetTemplate.user_id == user_id)
    if conditions:
        stmt = stmt.where(or_(*conditions))
    stmt = stmt.order_by(DatasetTemplate.is_default.desc(), DatasetTemplate.name.asc())
    result = await session.execute(stmt)
    return [_out(row) for row in result.scalars()]


async def get_template(session: AsyncSession, id: str) -> DatasetTemplateOut | None:
    row = await session.get(DatasetTemplate, id)
    if row is None:
        return None
    return _out(row)


async def get_default_template(session: AsyncSession) -> DatasetTemplateOut | None:
    stmt = select(DatasetTemplate).where(DatasetTemplate.is_default == True)
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        return None
    return _out(row)


async def create_template(
    session: AsyncSession, body: DatasetTemplateCreate, user_id: str | None = None
) -> DatasetTemplateOut:
    row = DatasetTemplate(
        name=body.name,
        collage_prompt=body.collage_prompt,
        user_id=user_id,
        description=body.description,
        collage_model=body.collage_model,
        collage_width=body.collage_width,
        collage_height=body.collage_height,
        collage_quality=body.collage_quality,
        split_grid_x=body.split_grid_x,
        split_grid_y=body.split_grid_y,
        upscale_enabled=body.upscale_enabled,
        upscale_model=body.upscale_model,
        target_megapixels=body.target_megapixels,
        upscale_enhance_details=body.upscale_enhance_details,
        upscale_realism=body.upscale_realism,
        caption_vision_model=body.caption_vision_model,
        caption_format=body.caption_format,
        is_default=body.is_default,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return _out(row)


async def update_template(
    session: AsyncSession,
    id: str,
    body: DatasetTemplateUpdate,
    user_id: str | None = None,
) -> DatasetTemplateOut | None:
    row = await session.get(DatasetTemplate, id)
    if row is None:
        return None
    if row.user_id != user_id:
        return None
    if body.name is not None:
        row.name = body.name
    if body.description is not None:
        row.description = body.description
    if body.collage_prompt is not None:
        row.collage_prompt = body.collage_prompt
    if body.collage_model is not None:
        row.collage_model = body.collage_model
    if body.collage_width is not None:
        row.collage_width = body.collage_width
    if body.collage_height is not None:
        row.collage_height = body.collage_height
    if body.collage_quality is not None:
        row.collage_quality = body.collage_quality
    if body.split_grid_x is not None:
        row.split_grid_x = body.split_grid_x
    if body.split_grid_y is not None:
        row.split_grid_y = body.split_grid_y
    if body.upscale_enabled is not None:
        row.upscale_enabled = body.upscale_enabled
    if body.upscale_model is not None:
        row.upscale_model = body.upscale_model
    if body.target_megapixels is not None:
        row.target_megapixels = body.target_megapixels
    if body.upscale_enhance_details is not None:
        row.upscale_enhance_details = body.upscale_enhance_details
    if body.upscale_realism is not None:
        row.upscale_realism = body.upscale_realism
    if body.caption_vision_model is not None:
        row.caption_vision_model = body.caption_vision_model
    if body.caption_format is not None:
        row.caption_format = body.caption_format
    if body.is_default is not None:
        row.is_default = body.is_default
    await session.commit()
    await session.refresh(row)
    return _out(row)


async def delete_template(
    session: AsyncSession, id: str, user_id: str | None = None
) -> bool | None:
    row = await session.get(DatasetTemplate, id)
    if row is None:
        return None
    if row.user_id != user_id:
        return None
    if row.is_default:
        return None
    await session.execute(delete(DatasetTemplate).where(DatasetTemplate.id == id))
    await session.commit()
    return True
