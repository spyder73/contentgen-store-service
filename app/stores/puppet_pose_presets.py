from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import PuppetPosePreset
from ..schemas import PuppetPosePresetCreate, PuppetPosePresetOut, PuppetPosePresetUpdate


class PuppetPosePresetConflict(Exception):
    pass


async def _name_taken(session: AsyncSession, user_id: str, name: str, *, exclude_id: str = "") -> bool:
    stmt = select(PuppetPosePreset.id).where(
        PuppetPosePreset.user_id == user_id, PuppetPosePreset.name == name
    )
    if exclude_id:
        stmt = stmt.where(PuppetPosePreset.id != exclude_id)
    return (await session.execute(stmt)).first() is not None


async def list_presets(session: AsyncSession, user_id: str) -> list[PuppetPosePresetOut]:
    rows = (await session.execute(
        select(PuppetPosePreset)
        .where(PuppetPosePreset.user_id == user_id)
        .order_by(PuppetPosePreset.name, PuppetPosePreset.created_at)
    )).scalars()
    return [PuppetPosePresetOut.model_validate(row) for row in rows]


async def get_preset(session: AsyncSession, id: str, user_id: str) -> PuppetPosePresetOut | None:
    row = (await session.execute(
        select(PuppetPosePreset).where(PuppetPosePreset.id == id, PuppetPosePreset.user_id == user_id)
    )).scalar_one_or_none()
    return PuppetPosePresetOut.model_validate(row) if row is not None else None


async def create_preset(
    session: AsyncSession, user_id: str, body: PuppetPosePresetCreate
) -> PuppetPosePresetOut:
    if await _name_taken(session, user_id, body.name):
        raise PuppetPosePresetConflict("a saved pose with this name already exists")
    row = PuppetPosePreset(
        user_id=user_id,
        name=body.name,
        prompt_hint=body.prompt_hint,
        config=body.config,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return PuppetPosePresetOut.model_validate(row)


async def update_preset(
    session: AsyncSession, id: str, user_id: str, body: PuppetPosePresetUpdate
) -> PuppetPosePresetOut | None:
    row = (await session.execute(
        select(PuppetPosePreset).where(PuppetPosePreset.id == id, PuppetPosePreset.user_id == user_id)
    )).scalar_one_or_none()
    if row is None:
        return None
    if body.name is not None and await _name_taken(session, user_id, body.name, exclude_id=id):
        raise PuppetPosePresetConflict("a saved pose with this name already exists")
    if body.name is not None:
        row.name = body.name
    if body.prompt_hint is not None:
        row.prompt_hint = body.prompt_hint
    if body.config is not None:
        row.config = body.config
    await session.commit()
    await session.refresh(row)
    return PuppetPosePresetOut.model_validate(row)


async def delete_preset(session: AsyncSession, id: str, user_id: str) -> bool:
    result = await session.execute(
        delete(PuppetPosePreset).where(PuppetPosePreset.id == id, PuppetPosePreset.user_id == user_id)
    )
    await session.commit()
    return bool(result.rowcount)
