from __future__ import annotations

from sqlalchemy import cast, delete, func, select, text, Text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

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
    source: str | None = None,
    page: int = 1,
    limit: int = 50,
    user_id: str | None = None,
) -> PagedResponse:
    if not user_id:
        raise ValueError("list_media requires user_id")
    offset = (page - 1) * limit
    # Defer the LargeBinary `file_data` BLOB: the list never serializes it
    # (MediaItemOut excludes it) so reading it per row only amplifies I/O and
    # TOAST de-toasting. The deferred column is never touched in this path —
    # MediaItemOut.from_orm_row reads no BLOB — so no lazy-load is triggered.
    query = (
        select(MediaItem)
        .where(MediaItem.user_id == user_id)
        .options(defer(MediaItem.file_data))
    )
    count_query = select(func.count()).select_from(MediaItem).where(MediaItem.user_id == user_id)

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
            | cast(MediaItem.id, Text).ilike(pattern)
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

    if source:
        filter_expr = MediaItem.metadata_["source"].astext == source
        query = query.where(filter_expr)
        count_query = count_query.where(filter_expr)

    count_result = await session.execute(count_query)
    total = count_result.scalar_one()
    result = await session.execute(
        query.order_by(MediaItem.created_at.desc()).offset(offset).limit(limit)
    )
    items = [MediaItemOut.from_orm_row(row) for row in result.scalars()]
    return PagedResponse(items=items, total=total, page=page, limit=limit)


async def _get_owned(session: AsyncSession, id: str, user_id: str) -> MediaItem | None:
    row = await session.get(MediaItem, id)
    if row is None:
        return None
    if row.user_id is not None and row.user_id != user_id:
        return None
    return row


async def get_media(
    session: AsyncSession, id: str, user_id: str | None = None
) -> MediaItemOut | None:
    if not user_id:
        raise ValueError("get_media requires user_id")
    row = await _get_owned(session, id, user_id)
    if row is None:
        return None
    return MediaItemOut.from_orm_row(row)


async def upsert_media(
    session: AsyncSession, body: MediaItemIn, user_id: str | None = None
) -> MediaItemOut:
    data = {
        "id": body.id,
        "clip_id": body.clip_id,
        "type": body.type,
        "prompt": body.prompt,
        "file_url": body.file_url,
        "metadata": body.metadata,
        "output_spec": body.output_spec,
        "name": body.name,
        "pipeline_run_id": body.pipeline_run_id,
        "scene_id": body.scene_id,
        "parent_media_id": body.parent_media_id,
        "role": body.role,
    }
    if user_id:
        data["user_id"] = user_id
    update_cols = {k: v for k, v in data.items() if k != "id"}
    stmt = (
        pg_insert(MediaItem.__table__)
        .values(**data)
        .on_conflict_do_update(index_elements=["id"], set_=update_cols)
    )
    await session.execute(stmt)
    await session.commit()
    row = await session.get(MediaItem, body.id, populate_existing=True)
    return MediaItemOut.from_orm_row(row)


async def delete_media(
    session: AsyncSession, id: str, user_id: str | None = None
) -> bool:
    if not user_id:
        raise ValueError("delete_media requires user_id")
    stmt = delete(MediaItem).where(
        MediaItem.id == id,
        (MediaItem.user_id == user_id) | (MediaItem.user_id.is_(None)),
    )
    result = await session.execute(stmt)
    await session.commit()
    return result.rowcount > 0


async def toggle_favourite(
    session: AsyncSession, id: str, is_favourite: bool, user_id: str | None = None
) -> MediaItemOut | None:
    if not user_id:
        raise ValueError("toggle_favourite requires user_id")
    row = await _get_owned(session, id, user_id)
    if row is None:
        return None
    row.is_favourite = is_favourite
    await session.commit()
    await session.refresh(row)
    return MediaItemOut.from_orm_row(row)


async def rename_media(
    session: AsyncSession, id: str, name: str, user_id: str | None = None
) -> MediaItemOut | None:
    if not user_id:
        raise ValueError("rename_media requires user_id")
    row = await _get_owned(session, id, user_id)
    if row is None:
        return None
    row.name = name
    await session.commit()
    await session.refresh(row)
    return MediaItemOut.from_orm_row(row)


async def store_file_data(
    session: AsyncSession, id: str, data: bytes, mime_type: str, user_id: str | None = None
) -> bool:
    if not user_id:
        raise ValueError("store_file_data requires user_id")
    row = await _get_owned(session, id, user_id)
    if row is None:
        return False
    row.file_data = data
    row.file_mime_type = mime_type
    await session.commit()
    return True


async def get_file_data(
    session: AsyncSession, id: str, user_id: str | None = None
) -> tuple[bytes, str] | None:
    if not user_id:
        raise ValueError("get_file_data requires user_id")
    row = await _get_owned(session, id, user_id)
    if row is None or row.file_data is None:
        return None
    return row.file_data, (row.file_mime_type or "application/octet-stream")


async def get_media_stats(session: AsyncSession, user_id: str | None = None) -> MediaStatsOut:
    query = select(MediaItem.type, func.count().label("cnt"))
    if user_id:
        query = query.where(MediaItem.user_id == user_id)
    query = query.group_by(MediaItem.type)
    result = await session.execute(query)
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
