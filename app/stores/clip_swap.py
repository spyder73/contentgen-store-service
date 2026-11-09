"""POST /v1/clips/{id}/swap: put another media item into one slot of a clip."""
from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from ..models import ClipPrompt, MediaItem
from ..schemas import ClipFullOut, SwapClipMediaBody
from ..uuids import canonical
from .clip_media import get_full_clip, is_foreign, may_use, media_owner

logger = logging.getLogger(__name__)

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
    session: AsyncSession, clip_id: str, body: SwapClipMediaBody, user_id: str | None = None
) -> ClipFullOut | None:
    row = await session.get(ClipPrompt, clip_id)
    if row is None or is_foreign(row, user_id):
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
            media_index = [canonical(b) for b in bucket].index(canonical(old_media_id))
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

    new_media_id = canonical(body.new_media_id)
    new_item = await session.get(MediaItem, new_media_id)
    # Media the clip's owner may not use is a missing media id: same branch, same
    # answer, with or without a header (see media_owner).
    owner = media_owner(row, user_id)
    if new_item is not None and not may_use(new_item.user_id, owner):
        new_item = None
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

    bucket[media_index] = new_media_id
    media_refs[kind_key] = bucket
    row.media_refs = media_refs
    row.is_dirty = True
    await session.commit()
    await session.refresh(row)
    return await get_full_clip(session, clip_id, user_id=user_id)
