from __future__ import annotations

from sqlalchemy import cast, delete, func, select, text, Text, update
from sqlalchemy.dialects.postgresql import JSONB, insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from ..derivatives import is_image_content_type, make_micro_thumbnail, make_thumbnail
from ..models import MediaItem
from ..schemas import (
    MediaItemIn,
    MediaItemOut,
    MediaItemPatch,
    MediaStatsOut,
    PagedResponse,
    RelatedMediaOut,
)


# "Uploaded" vs "generated" is not a single literal source value. Manual uploads
# write metadata.source in this set; everything else (generated, render_output,
# the legacy "persisted" value, or no source at all) is treated as "generated".
# Mirrors the frontend's `isGeneratedSource` heuristic so the server-side filter
# and the client's mental model agree.
_UPLOAD_SOURCES = ("manual_upload", "upload", "upload_pool", "uploaded")

# Generated videos are stored with type "ai_video", but the library's type chip
# (and the per-type facet counts) speak of "video". Bucket both raw column values
# under the "video" facet so a ``type=video`` filter and the video count include
# AI-generated videos (otherwise "0 videos" shows even when ai_video rows exist).
_TYPE_BUCKETS: dict[str, tuple[str, ...]] = {
    "video": ("video", "ai_video"),
}


def _type_bucket_for(raw_type: str | None) -> str | None:
    """Map a raw ``type`` column value to its facet bucket (image/video/audio)."""
    if raw_type in ("video", "ai_video"):
        return "video"
    return raw_type


def _type_filter_expr(type_: str):
    """WHERE expression for a type facet — expands ``video`` to also match the
    ``ai_video`` rows generated videos are stored under."""
    values = _TYPE_BUCKETS.get(type_)
    if values:
        return MediaItem.type.in_(values)
    return MediaItem.type == type_


def _source_filter_expr(source: str):
    """Return a WHERE expression for the ``source`` bucket / literal.

    - ``uploaded`` → metadata.source IN the upload set.
    - ``generated`` → metadata.source NOT IN the upload set (includes NULL /
      missing source, so legacy rows count as generated rather than vanishing).
    - any other value → exact match on metadata.source (back-compat for callers
      that pass a concrete source string).
    """
    src = MediaItem.metadata_["source"].astext
    if source == "uploaded":
        return src.in_(_UPLOAD_SOURCES)
    if source == "generated":
        return src.is_(None) | src.notin_(_UPLOAD_SOURCES)
    return src == source


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
    generator_profile_id: str | None = None,
    page: int = 1,
    limit: int = 50,
    user_id: str | None = None,
) -> PagedResponse:
    if not user_id:
        raise ValueError("list_media requires user_id")
    offset = (page - 1) * limit
    # Defer the LargeBinary `file_data` and `thumbnail_data` BLOBs: the list
    # never serializes them (MediaItemOut excludes both) so reading them per row
    # only amplifies I/O and TOAST de-toasting. The deferred columns are never
    # touched in this path — MediaItemOut.from_orm_row reads only the small
    # mime-type columns — so no lazy-load is triggered.
    query = (
        select(MediaItem)
        .where(MediaItem.user_id == user_id)
        .options(defer(MediaItem.file_data), defer(MediaItem.thumbnail_data))
    )
    count_query = select(func.count()).select_from(MediaItem).where(MediaItem.user_id == user_id)

    if clip_id:
        query = query.where(MediaItem.clip_id == clip_id)
        count_query = count_query.where(MediaItem.clip_id == clip_id)
    if type_:
        type_expr = _type_filter_expr(type_)
        query = query.where(type_expr)
        count_query = count_query.where(type_expr)
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
        filter_expr = _source_filter_expr(source)
        query = query.where(filter_expr)
        count_query = count_query.where(filter_expr)

    if generator_profile_id:
        gen_profile_filter = MediaItem.metadata_["generator_profile_id"].astext == generator_profile_id
        query = query.where(gen_profile_filter)
        count_query = count_query.where(gen_profile_filter)

    count_result = await session.execute(count_query)
    total = count_result.scalar_one()
    # The secondary `id` key makes the order TOTAL: ``created_at DESC`` alone is
    # ambiguous when many rows share a timestamp (a batch written in one
    # transaction), and Postgres may then return tied rows in a different order
    # per LIMIT/OFFSET query — so the same row can resurface on a later page
    # (page-1 items leaking into page N). Breaking the tie by `id DESC` makes
    # every page a deterministic slice of one global order: disjoint pages, no
    # duplicates. (See ix_media_items_user_created_desc — the index is on
    # user_id + created_at; id is appended in the sort, not the index.)
    result = await session.execute(
        query.order_by(MediaItem.created_at.desc(), MediaItem.id.desc())
        .offset(offset)
        .limit(limit)
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


async def _get_by_id(session: AsyncSession, id: str, user_id: str | None) -> MediaItem | None:
    """Fetch a media row by id, optionally scoped to ``user_id``.

    Used by the byte-serving routes (file/thumbnail): those sit behind the
    X-Internal-Secret gate and are reached via the go-backend's public,
    unauthenticated embed path, which has no user context to forward. There
    the UUID id itself is the access control, so ``user_id=None`` returns
    the row unfiltered. When a caller DOES supply ``user_id`` it's still
    honored as an extra ownership filter, preserving the scoped lookup used
    by authenticated store callers (e.g. `_get_owned`).
    """
    row = await session.get(MediaItem, id)
    if row is None:
        return None
    if user_id and row.user_id is not None and row.user_id != user_id:
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


# Cap how many siblings/variations the lineage view returns — the inspector
# shows a row of thumbnails, not an unbounded gallery.
_LINEAGE_LIMIT = 24


async def get_related_media(
    session: AsyncSession, id: str, user_id: str | None = None
) -> RelatedMediaOut | None:
    """Return the lineage of a media item: its parent, its co-variation siblings
    (sharing the same ``parent_media_id``), and the variations derived from it
    (rows whose ``parent_media_id`` points back at it).

    All results are scoped to ``user_id`` and exclude the item itself. Returns
    None when the item does not exist / is not owned; an item with no lineage
    yields an empty ``RelatedMediaOut`` (no parent, empty lists). BLOBs are
    deferred — the inspector renders thumbnails, never the originals.
    """
    if not user_id:
        raise ValueError("get_related_media requires user_id")
    row = await _get_owned(session, id, user_id)
    if row is None:
        return None

    deferred = (defer(MediaItem.file_data), defer(MediaItem.thumbnail_data))

    parent_out: MediaItemOut | None = None
    siblings: list[MediaItemOut] = []
    if row.parent_media_id:
        parent_row = await _get_owned(session, row.parent_media_id, user_id)
        if parent_row is not None:
            parent_out = MediaItemOut.from_orm_row(parent_row)
        # Siblings: same parent, not this item.
        sib_q = (
            select(MediaItem)
            .where(
                MediaItem.user_id == user_id,
                MediaItem.parent_media_id == row.parent_media_id,
                MediaItem.id != id,
            )
            .options(*deferred)
            .order_by(MediaItem.created_at.desc(), MediaItem.id.desc())
            .limit(_LINEAGE_LIMIT)
        )
        sib_res = await session.execute(sib_q)
        siblings = [MediaItemOut.from_orm_row(r) for r in sib_res.scalars()]

    # Variations: rows whose parent is this item.
    var_q = (
        select(MediaItem)
        .where(
            MediaItem.user_id == user_id,
            MediaItem.parent_media_id == id,
        )
        .options(*deferred)
        .order_by(MediaItem.created_at.desc(), MediaItem.id.desc())
        .limit(_LINEAGE_LIMIT)
    )
    var_res = await session.execute(var_q)
    variations = [MediaItemOut.from_orm_row(r) for r in var_res.scalars()]

    return RelatedMediaOut(parent=parent_out, siblings=siblings, variations=variations)


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


async def patch_media(
    session: AsyncSession,
    id: str,
    body: MediaItemPatch,
    user_id: str | None = None,
) -> MediaItemOut | None:
    """Atomically patch one owned media row without replacing unrelated data."""
    if not user_id:
        raise ValueError("patch_media requires user_id")

    values: dict[object, object] = {}
    if body.file_url is not None:
        values[MediaItem.file_url] = body.file_url
    if body.metadata_merge:
        # PostgreSQL JSONB || performs a shallow key merge in the same UPDATE as
        # file_url. coalesce protects legacy NULL metadata rows. This is the
        # concurrency boundary required by the independent byte-persistence and
        # credit-settlement goroutines in the Go backend.
        values[MediaItem.metadata_] = func.coalesce(
            MediaItem.metadata_,
            cast({}, JSONB),
        ).op("||")(cast(body.metadata_merge, JSONB))

    if not values:
        return await get_media(session, id, user_id=user_id)

    stmt = (
        update(MediaItem)
        .where(
            MediaItem.id == id,
            (MediaItem.user_id == user_id) | (MediaItem.user_id.is_(None)),
        )
        .values(values)
        .returning(MediaItem.id)
    )
    result = await session.execute(stmt)
    if result.scalar_one_or_none() is None:
        await session.rollback()
        return None
    await session.commit()
    row = await session.get(MediaItem, id, populate_existing=True)
    if row is None:
        return None
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
    # Eagerly derive the grid thumbnail for images so the library never has to
    # load the full original. Non-images / undecodable bytes yield None → the
    # grid falls back to the original (no thumbnail advertised).
    if is_image_content_type(mime_type):
        thumb = make_thumbnail(data, mime_type)
        if thumb is not None:
            row.thumbnail_data, row.thumbnail_content_type = thumb
        else:
            # Clear any stale derivative if the new bytes can't be thumbnailed
            # (e.g. already small enough, or a re-upload with a different format).
            row.thumbnail_data = None
            row.thumbnail_content_type = None
        # The micro-thumb blur-up placeholder is produced even for small images
        # (it's a placeholder, not a payload optimisation). None → clear it.
        row.micro_thumbnail = make_micro_thumbnail(data, mime_type)
    else:
        row.micro_thumbnail = None
    await session.commit()
    return True


async def get_thumbnail(
    session: AsyncSession, id: str, user_id: str | None = None
) -> tuple[bytes, str] | None:
    """Return ``(bytes, content_type)`` for the item's grid thumbnail.

    ``user_id`` is optional here (unlike most other media accessors): this
    backs a byte-serving route reached via the go-backend's public embed
    path with no user context, where the id itself is the access gate. When
    ``user_id`` is supplied it's still enforced as an ownership filter — see
    `_get_by_id`.

    Lazy backfill: if no derivative exists yet but the row is an image with
    stored bytes, generate the thumbnail on this first GET and persist it so the
    next request is served from the column. Returns None when no thumbnail can
    be produced (caller falls back to the original).
    """
    row = await _get_by_id(session, id, user_id)
    if row is None:
        return None
    if row.thumbnail_data is not None and row.thumbnail_content_type:
        # Opportunistically backfill the micro-thumb for legacy rows that have a
        # full thumbnail but predate the 0018 column. Cheap (a few hundred bytes)
        # and saves the list path from ever needing the BLOB.
        if row.micro_thumbnail is None and row.file_data is not None:
            micro = make_micro_thumbnail(row.file_data, row.file_mime_type)
            if micro is not None:
                row.micro_thumbnail = micro
                await session.commit()
        return row.thumbnail_data, row.thumbnail_content_type
    # No derivative yet — attempt lazy backfill from the stored original.
    if row.file_data is None or not is_image_content_type(row.file_mime_type):
        return None
    thumb = make_thumbnail(row.file_data, row.file_mime_type)
    if thumb is None:
        # Even when no full thumbnail is warranted (image already small enough),
        # the micro-thumb placeholder is still useful — backfill it lazily.
        if row.micro_thumbnail is None:
            micro = make_micro_thumbnail(row.file_data, row.file_mime_type)
            if micro is not None:
                row.micro_thumbnail = micro
                await session.commit()
        return None
    row.thumbnail_data, row.thumbnail_content_type = thumb
    if row.micro_thumbnail is None:
        row.micro_thumbnail = make_micro_thumbnail(row.file_data, row.file_mime_type)
    await session.commit()
    return thumb


async def get_file_data(
    session: AsyncSession, id: str, user_id: str | None = None
) -> tuple[bytes, str] | None:
    """Return ``(bytes, mime_type)`` for the item's stored original.

    ``user_id`` is optional — see `get_thumbnail` / `_get_by_id` for why this
    byte-serving accessor is not ownership-required like the rest of the
    media store.
    """
    row = await _get_by_id(session, id, user_id)
    if row is None or row.file_data is None:
        return None
    return row.file_data, (row.file_mime_type or "application/octet-stream")


async def get_media_stats(session: AsyncSession, user_id: str | None = None) -> MediaStatsOut:
    # Library-wide counts per type AND per source bucket. These are the canonical
    # facet totals the UI shows on the type/source chips — derived here over the
    # WHOLE library so a type/source absent from the current page never reads as
    # "0 videos". Counting both facets in one grouped pass keeps it a single scan.
    src = MediaItem.metadata_["source"].astext
    query = select(MediaItem.type, src.label("source"), func.count().label("cnt"))
    if user_id:
        query = query.where(MediaItem.user_id == user_id)
    query = query.group_by(MediaItem.type, src)
    result = await session.execute(query)

    type_counts: dict[str, int] = {}
    uploaded = 0
    generated = 0
    total = 0
    for row in result:
        # Fold ai_video into the "video" facet so generated videos are counted.
        bucket = _type_bucket_for(row.type)
        if bucket:
            type_counts[bucket] = type_counts.get(bucket, 0) + row.cnt
        if row.source in _UPLOAD_SOURCES:
            uploaded += row.cnt
        else:  # generated bucket: everything else, including NULL/missing source
            generated += row.cnt
        total += row.cnt

    return MediaStatsOut(
        total=total,
        image=type_counts.get("image", 0),
        video=type_counts.get("video", 0),
        audio=type_counts.get("audio", 0),
        uploaded=uploaded,
        generated=generated,
    )
