from __future__ import annotations

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import MediaItem
from ..schemas import MediaItemIn, MediaItemOut, PagedResponse


async def list_media(
    session: AsyncSession,
    clip_id: str | None = None,
    type_: str | None = None,
    search: str | None = None,
    page: int = 1,
    limit: int = 50,
) -> PagedResponse:
    offset = (page - 1) * limit
    query = select(MediaItem)
    count_query = select(func.count()).select_from(MediaItem)
    if clip_id:
        query = query.where(MediaItem.clip_id == clip_id)
        count_query = count_query.where(MediaItem.clip_id == clip_id)
    if type_:
        query = query.where(MediaItem.type == type_)
        count_query = count_query.where(MediaItem.type == type_)
    if search:
        pattern = f"%{search}%"
        search_filter = MediaItem.prompt.ilike(pattern) | MediaItem.id.ilike(pattern)
        query = query.where(search_filter)
        count_query = count_query.where(search_filter)
    count_result = await session.execute(count_query)
    total = count_result.scalar_one()
    result = await session.execute(
        query.order_by(MediaItem.created_at.desc()).offset(offset).limit(limit)
    )
    items = [MediaItemOut.from_orm_row(row) for row in result.scalars()]
    return PagedResponse(items=items, total=total, page=page, limit=limit)


async def get_media(session: AsyncSession, id: str) -> MediaItemOut | None:
    row = await session.get(MediaItem, id)
    if row is None:
        return None
    return MediaItemOut.from_orm_row(row)


async def upsert_media(session: AsyncSession, body: MediaItemIn) -> MediaItemOut:
    row = await session.get(MediaItem, body.id)
    if row is None:
        row = MediaItem(id=body.id)
        session.add(row)
    row.clip_id = body.clip_id
    row.type = body.type
    row.prompt = body.prompt
    row.file_url = body.file_url
    row.metadata_ = body.metadata
    row.output_spec = body.output_spec
    await session.commit()
    await session.refresh(row)
    return MediaItemOut.from_orm_row(row)


async def delete_media(session: AsyncSession, id: str) -> bool:
    result = await session.execute(delete(MediaItem).where(MediaItem.id == id))
    await session.commit()
    return result.rowcount > 0
