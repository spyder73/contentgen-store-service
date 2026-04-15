from __future__ import annotations

from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import MediaItem
from ..schemas import MediaItemIn, MediaItemOut, MediaStatsOut, PagedResponse


async def list_media(
    session: AsyncSession,
    clip_id: str | None = None,
    type_: str | None = None,
    search: str | None = None,
    is_favourite: bool | None = None,
    pipeline_run_id: str | None = None,
    scene_id: str | None = None,
    role: str | None = None,
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
        search_filter = (
            MediaItem.prompt.ilike(pattern)
            | MediaItem.id.ilike(pattern)
            | MediaItem.name.ilike(pattern)
        )
        query = query.where(search_filter)
        count_query = count_query.where(search_filter)
    if is_favourite is not None:
        query = query.where(MediaItem.is_favourite == is_favourite)
        count_query = count_query.where(MediaItem.is_favourite == is_favourite)
    if pipeline_run_id:
        query = query.where(MediaItem.pipeline_run_id == pipeline_run_id)
        count_query = count_query.where(MediaItem.pipeline_run_id == pipeline_run_id)
    if scene_id:
        query = query.where(MediaItem.scene_id == scene_id)
        count_query = count_query.where(MediaItem.scene_id == scene_id)
    if role:
        query = query.where(MediaItem.role == role)
        count_query = count_query.where(MediaItem.role == role)

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
    row.name = body.name
    row.pipeline_run_id = body.pipeline_run_id
    row.scene_id = body.scene_id
    row.parent_media_id = body.parent_media_id
    row.role = body.role
    await session.commit()
    await session.refresh(row)
    return MediaItemOut.from_orm_row(row)


async def delete_media(session: AsyncSession, id: str) -> bool:
    result = await session.execute(delete(MediaItem).where(MediaItem.id == id))
    await session.commit()
    return result.rowcount > 0


async def toggle_favourite(session: AsyncSession, id: str, is_favourite: bool) -> MediaItemOut | None:
    row = await session.get(MediaItem, id)
    if row is None:
        return None
    row.is_favourite = is_favourite
    await session.commit()
    await session.refresh(row)
    return MediaItemOut.from_orm_row(row)


async def rename_media(session: AsyncSession, id: str, name: str) -> MediaItemOut | None:
    row = await session.get(MediaItem, id)
    if row is None:
        return None
    row.name = name
    await session.commit()
    await session.refresh(row)
    return MediaItemOut.from_orm_row(row)


async def get_media_stats(session: AsyncSession) -> MediaStatsOut:
    result = await session.execute(
        select(MediaItem.type, func.count().label("cnt")).group_by(MediaItem.type)
    )
    counts: dict[str, int] = {}
    for row in result:
        counts[row.type] = row.cnt
    total = sum(counts.values())
    return MediaStatsOut(
        total=total,
        image=counts.get("image", 0),
        video=counts.get("video", 0),
        audio=counts.get("audio", 0),
    )
