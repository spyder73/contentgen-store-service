from __future__ import annotations

from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import ClipPrompt, MediaItem
from ..schemas import (
    ClipFullOut,
    ClipPromptIn,
    ClipPromptOut,
    ClipSummaryOut,
    MediaItemOut,
    PagedResponse,
    SwapClipMediaBody,
)


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


async def list_clip_summaries(
    session: AsyncSession,
    page: int = 1,
    limit: int = 50,
    finished_only: bool = False,
) -> PagedResponse:
    offset = (page - 1) * limit
    count_query = select(func.count()).select_from(ClipPrompt)
    query = select(ClipPrompt)
    if finished_only:
        count_query = count_query.where(ClipPrompt.finished_at.isnot(None))
        query = query.where(ClipPrompt.finished_at.isnot(None))
    count_result = await session.execute(count_query)
    total = count_result.scalar_one()
    result = await session.execute(
        query.order_by(ClipPrompt.created_at.desc()).offset(offset).limit(limit)
    )
    items = [
        ClipSummaryOut(
            id=row.id,
            name=row.name,
            created_at=row.created_at,
            updated_at=row.updated_at,
            finished_at=row.finished_at,
            thumbnail_url=row.thumbnail_url,
            is_dirty=row.is_dirty or False,
            media_count={
                "images": len((row.media_refs or {}).get("images", [])),
                "ai_videos": len((row.media_refs or {}).get("ai_videos", [])),
                "audios": len((row.media_refs or {}).get("audios", [])),
            },
        )
        for row in result.scalars()
    ]
    return PagedResponse(items=items, total=total, page=page, limit=limit)


async def get_clip(session: AsyncSession, id: str) -> ClipPromptOut | None:
    row = await session.get(ClipPrompt, id)
    if row is None:
        return None
    return ClipPromptOut.from_orm_row(row)


async def get_full_clip(session: AsyncSession, id: str) -> ClipFullOut | None:
    row = await session.get(ClipPrompt, id)
    if row is None:
        return None
    clip_out = ClipPromptOut.from_orm_row(row)
    media_refs = row.media_refs or {"images": [], "ai_videos": [], "audios": []}
    all_ids = (
        media_refs.get("images", [])
        + media_refs.get("ai_videos", [])
        + media_refs.get("audios", [])
    )
    media_items: list[MediaItemOut] = []
    if all_ids:
        result = await session.execute(
            select(MediaItem).where(MediaItem.id.in_(all_ids))
        )
        media_items = [MediaItemOut.from_orm_row(m) for m in result.scalars()]
    return ClipFullOut(clip=clip_out, media=media_items)


async def upsert_clip(session: AsyncSession, body: ClipPromptIn) -> ClipPromptOut:
    row = await session.get(ClipPrompt, body.id)
    if row is None:
        row = ClipPrompt(id=body.id)
        session.add(row)
    row.name = body.name
    row.metadata_ = body.metadata
    row.style = body.style
    row.media_refs = body.media_refs
    row.render_output_urls = body.render_output_urls
    row.is_dirty = body.is_dirty
    row.finished_at = body.finished_at
    row.thumbnail_url = body.thumbnail_url
    await session.commit()
    await session.refresh(row)
    return ClipPromptOut.from_orm_row(row)


async def swap_clip_media(
    session: AsyncSession, clip_id: str, body: SwapClipMediaBody
) -> ClipFullOut | None:
    row = await session.get(ClipPrompt, clip_id)
    if row is None:
        return None

    kind_map = {"image": "images", "ai_video": "ai_videos", "audio": "audios"}
    kind_key = kind_map.get(body.kind)
    if kind_key is None:
        raise ValueError(f"Unknown kind '{body.kind}'. Must be one of: image, ai_video, audio")

    media_refs = dict(row.media_refs or {"images": [], "ai_videos": [], "audios": []})
    bucket: list = list(media_refs.get(kind_key, []))

    if body.media_index < 0 or body.media_index >= len(bucket):
        raise IndexError(
            f"media_index {body.media_index} out of range for '{kind_key}' (len={len(bucket)})"
        )

    new_item = await session.get(MediaItem, body.new_media_id)
    if new_item is None:
        raise LookupError(f"media item '{body.new_media_id}' not found")

    bucket[body.media_index] = body.new_media_id
    media_refs[kind_key] = bucket
    row.media_refs = media_refs
    row.is_dirty = True
    await session.commit()
    await session.refresh(row)
    return await get_full_clip(session, clip_id)


async def delete_clip(session: AsyncSession, id: str) -> bool:
    result = await session.execute(delete(ClipPrompt).where(ClipPrompt.id == id))
    await session.commit()
    return result.rowcount > 0
