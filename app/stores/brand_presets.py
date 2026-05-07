from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import BrandPreset
from ..schemas import BrandPresetIn, BrandPresetOut, BrandPresetPatch


async def list_brand_presets(
    session: AsyncSession, *, user_id: str, clip_style: str | None = None
) -> list[BrandPresetOut]:
    stmt = select(BrandPreset).where(BrandPreset.user_id == user_id)
    if clip_style:
        stmt = stmt.where(BrandPreset.clip_style == clip_style)
    stmt = stmt.order_by(BrandPreset.clip_style, BrandPreset.name)
    result = await session.execute(stmt)
    return [BrandPresetOut.from_orm_row(row) for row in result.scalars()]


async def create_brand_preset(
    session: AsyncSession, body: BrandPresetIn, *, user_id: str
) -> BrandPresetOut:
    row = BrandPreset(
        user_id=user_id,
        clip_style=body.clip_style.strip(),
        name=body.name.strip(),
        brand_tag=body.brand_tag.strip(),
        preset_json=body.preset_json or {},
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return BrandPresetOut.from_orm_row(row)


async def update_brand_preset(
    session: AsyncSession, id: str, body: BrandPresetPatch, *, user_id: str
) -> BrandPresetOut | None:
    row = await session.get(BrandPreset, id)
    if row is None or row.user_id != user_id:
        return None

    if body.name is not None:
        row.name = body.name.strip()
    if body.brand_tag is not None:
        row.brand_tag = body.brand_tag.strip()
    if body.preset_json is not None:
        row.preset_json = body.preset_json

    await session.commit()
    await session.refresh(row)
    return BrandPresetOut.from_orm_row(row)


async def delete_brand_preset(session: AsyncSession, id: str, *, user_id: str) -> bool:
    result = await session.execute(
        delete(BrandPreset).where(BrandPreset.id == id, BrandPreset.user_id == user_id)
    )
    await session.commit()
    return result.rowcount > 0
