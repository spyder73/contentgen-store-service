from __future__ import annotations

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import ClipPrompt
from ..schemas import ClipPromptIn, ClipPromptOut, PagedResponse


async def list_clips(
    session: AsyncSession, page: int = 1, limit: int = 50
) -> PagedResponse:
    offset = (page - 1) * limit
    count_result = await session.execute(select(func.count()).select_from(ClipPrompt))
    total = count_result.scalar_one()
    result = await session.execute(
        select(ClipPrompt).order_by(ClipPrompt.created_at.desc()).offset(offset).limit(limit)
    )
    items = [ClipPromptOut.from_orm_row(row) for row in result.scalars()]
    return PagedResponse(items=items, total=total, page=page, limit=limit)


async def get_clip(session: AsyncSession, id: str) -> ClipPromptOut | None:
    row = await session.get(ClipPrompt, id)
    if row is None:
        return None
    return ClipPromptOut.from_orm_row(row)


async def upsert_clip(session: AsyncSession, body: ClipPromptIn) -> ClipPromptOut:
    row = await session.get(ClipPrompt, body.id)
    if row is None:
        row = ClipPrompt(id=body.id)
        session.add(row)
    row.name = body.name
    row.metadata_ = body.metadata
    row.style = body.style
    await session.commit()
    await session.refresh(row)
    return ClipPromptOut.from_orm_row(row)


async def delete_clip(session: AsyncSession, id: str) -> bool:
    result = await session.execute(delete(ClipPrompt).where(ClipPrompt.id == id))
    await session.commit()
    return result.rowcount > 0
