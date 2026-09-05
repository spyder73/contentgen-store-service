from __future__ import annotations

import logging

from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

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


def _user_filter(user_id: str):
    """Return a WHERE clause for user_id filtering. user_id is required."""
    if not user_id:
        raise ValueError("user_id is required for clip listing")
    return ClipPrompt.user_id == user_id


async def list_clips(
    session: AsyncSession, *, user_id: str, page: int = 1, limit: int = 50
) -> PagedResponse:
    offset = (page - 1) * limit
    filt = _user_filter(user_id)
    count_result = await session.execute(select(func.count()).select_from(ClipPrompt).where(filt))
    total = count_result.scalar_one()
    result = await session.execute(
        select(ClipPrompt).where(filt).order_by(ClipPrompt.created_at.desc()).offset(offset).limit(limit)
    )
    items = [ClipPromptOut.from_orm_row(row) for row in result.scalars()]
    return PagedResponse(items=items, total=total, page=page, limit=limit)


async def list_clip_summaries(
    session: AsyncSession,
    *,
    user_id: str,
    page: int = 1,
    limit: int = 50,
    finished_only: bool = False,
    unfinished_only: bool = False,
    search: str | None = None,
) -> PagedResponse:
    offset = (page - 1) * limit
    where = [_user_filter(user_id)]
    if finished_only:
        where.append(ClipPrompt.finished_at.isnot(None))
    elif unfinished_only:
        where.append(ClipPrompt.finished_at.is_(None))
    if search:
        where.append(ClipPrompt.name.ilike(f"%{search}%"))
    count_query = select(func.count()).select_from(ClipPrompt)
    query = select(ClipPrompt)
    for w in where:
        count_query = count_query.where(w)
        query = query.where(w)
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
            style=row.style or {},
            render_output_urls=row.render_output_urls or [],
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
    # NOTE: deliberately no ORDER BY on the `id.in_(all_ids)` query below —
    # Postgres does not guarantee IN-list order, so a caller computing a
    # POSITIONAL media_index off this response's array can drift from
    # media_refs' own bucket order (this was a live hazard: swap_clip_media
    # was positional-only). Fixing the query's ordering was consciously
    # skipped here: it is a wide-blast-radius change (every consumer of this
    # endpoint), and SwapClipMediaBody.old_media_id below removes the
    # load-bearing need for order to matter — a slot resolved BY ID cannot be
    # the wrong one regardless of what order this array comes back in.
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


async def upsert_clip(
    session: AsyncSession, body: ClipPromptIn, user_id: str | None = None
) -> ClipPromptOut:
    row = await session.get(ClipPrompt, body.id)
    if row is None:
        row = ClipPrompt(id=body.id)
        if user_id:
            row.user_id = user_id
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


async def patch_clip(session: AsyncSession, id: str, fields: dict) -> ClipPromptOut | None:
    """Write only the keys present in ``fields`` (from ``model_dump(exclude_unset=True)``
    on ``ClipPromptPatch``). Unlike ``upsert_clip``, an omitted key leaves its column
    untouched — that is the whole point: a metadata-only patch must never default
    finished_at/thumbnail_url/render_output_urls back to None/[] the way the PUT's
    unconditional upsert does. ``metadata`` is remapped to the ORM's ``metadata_``
    attribute (the model column is named "metadata" in Postgres but "metadata" is a
    reserved attribute name on the Declarative base).
    """
    row = await session.get(ClipPrompt, id)
    if row is None:
        return None
    for key, value in fields.items():
        attr = "metadata_" if key == "metadata" else key
        setattr(row, attr, value)
    await session.commit()
    await session.refresh(row)
    return ClipPromptOut.from_orm_row(row)


# A bucket's kind must agree with the TYPE of the media item being swapped
# into it: "ai_video" is stored as "ai_video" going forward but "video" on
# legacy rows (see app/stores/media.py's own _TYPE_BUCKETS note on the same
# split), so ai_video tolerates both; image/audio are exact. Enforced
# unconditionally in swap_clip_media, on both the id-based and positional
# paths — kills the latent hazard where ClipFullDTOToClipPrompt (the Go
# backend's viewModel.go) re-buckets by media TYPE rather than by which
# media_refs bucket an id lives in: a wrong-typed id in a bucket would
# silently re-bucket to a different slide kind on the next read.
_KIND_TYPE_ALIASES: dict[str, tuple[str, ...]] = {
    "image": ("image",),
    "ai_video": ("ai_video", "video"),
    "audio": ("audio",),
}


async def swap_clip_media(
    session: AsyncSession, clip_id: str, body: SwapClipMediaBody
) -> ClipFullOut | None:
    row = await session.get(ClipPrompt, clip_id)
    if row is None:
        logger.warning("swap_clip_media: clip_id=%s not found in DB", clip_id)
        return None

    kind_map = {"image": "images", "ai_video": "ai_videos", "audio": "audios"}
    kind_key = kind_map.get(body.kind)
    if kind_key is None:
        raise ValueError(f"Unknown kind '{body.kind}'. Must be one of: image, ai_video, audio")

    media_refs = dict(row.media_refs or {"images": [], "ai_videos": [], "audios": []})
    bucket: list = list(media_refs.get(kind_key, []))

    # Resolve WHICH slot swaps: by id when the caller names the row it means
    # to replace, positionally otherwise (the legacy fallback). Id-based
    # resolution is immune to get_full_clip's lack of an ORDER BY (see that
    # function's own comment): a media_index computed off a differently- or
    # non-deterministically-ordered read can name the wrong slot; an id
    # cannot, because bucket.index() searches the store's OWN current order
    # rather than trusting the caller's.
    old_media_id = (body.old_media_id or "").strip()
    if old_media_id:
        try:
            media_index = bucket.index(old_media_id)
        except ValueError:
            raise ValueError(
                f"old_media_id '{old_media_id}' not found in '{kind_key}' bucket for clip {clip_id}"
            ) from None
    else:
        media_index = body.media_index
        if media_index < 0 or media_index >= len(bucket):
            raise IndexError(
                f"media_index {media_index} out of range for '{kind_key}' (len={len(bucket)})"
            )

    new_item = await session.get(MediaItem, body.new_media_id)
    if new_item is None:
        logger.warning(
            "swap_clip_media: media_item=%s not found in DB (clip=%s, kind=%s, index=%d)",
            body.new_media_id, clip_id, body.kind, media_index,
        )
        raise LookupError(f"media item '{body.new_media_id}' not found")

    allowed_types = _KIND_TYPE_ALIASES.get(body.kind, ())
    if new_item.type not in allowed_types:
        raise ValueError(
            f"media item '{body.new_media_id}' has type '{new_item.type}', which does not "
            f"match kind '{body.kind}' ('{kind_key}' bucket)"
        )

    bucket[media_index] = body.new_media_id
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
