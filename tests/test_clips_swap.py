"""swap_clip_media: id-based slot resolution + type-vs-bucket validation.

W2a hardening. Two latent hazards this closes:

  - get_full_clip's media array has no ORDER BY (deliberately — see its own
    comment), so a caller computing media_index off that array's order can
    name the WRONG slot once the array's order drifts from media_refs' own
    bucket order. Naming the slot's CURRENT occupant by id instead cannot
    drift the same way — swap_clip_media resolves the index itself, from the
    store's own bucket, not the caller's.
  - swap_clip_media never validated that the new item's type actually belongs
    in the bucket kind it is being swapped into (image -> images, ai_video ->
    ai_videos, audio -> audios). A caller sending the wrong id for the bucket
    would silently rebind a slide to a mismatched media type.

Every case here exercises the store function directly with a mocked
AsyncSession (see tests/test_media_patch.py for the same pattern), not the
HTTP layer: no real Postgres needed.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models import ClipPrompt, MediaItem
from app.schemas import SwapClipMediaBody
from app.stores import clips as clips_store


def _clip_row(clip_id: str, media_refs: dict, is_dirty: bool = False) -> SimpleNamespace:
    now = datetime.now(timezone.utc)
    return SimpleNamespace(
        id=clip_id,
        name="a clip",
        metadata_={},
        style={},
        media_refs=media_refs,
        render_output_urls=[],
        is_dirty=is_dirty,
        finished_at=None,
        thumbnail_url=None,
        created_at=now,
        updated_at=now,
    )


def _media_row(media_id: str, type_: str) -> SimpleNamespace:
    now = datetime.now(timezone.utc)
    return SimpleNamespace(
        id=media_id,
        clip_id=None,
        type=type_,
        prompt="a prompt",
        file_url=f"/media/uploads/{media_id}.png",
        metadata_={},
        output_spec=None,
        is_favourite=False,
        name="",
        pipeline_run_id=None,
        scene_id=None,
        parent_media_id=None,
        role=None,
        file_mime_type="image/png",
        thumbnail_content_type=None,
        micro_thumbnail=None,
        created_at=now,
        updated_at=now,
    )


def _session_with(clip_row: SimpleNamespace, media_rows: dict[str, SimpleNamespace]) -> MagicMock:
    """A MagicMock AsyncSession serving one clip row and a pool of media rows.

    session.get is dispatched by model class, the same way SQLAlchemy's real
    AsyncSession.get would resolve either kind of primary-key lookup
    swap_clip_media and get_full_clip perform (ClipPrompt by clip id, MediaItem
    by media id). session.execute stands in for get_full_clip's
    ``select(MediaItem).where(MediaItem.id.in_(all_ids))`` — deliberately
    ignoring the WHERE clause and returning every fixture row, since ordering
    (or lack of it) is exactly what this suite does not depend on.
    """
    session = MagicMock()

    async def _get(model, id_):
        if model is ClipPrompt:
            return clip_row if id_ == clip_row.id else None
        if model is MediaItem:
            return media_rows.get(id_)
        return None

    session.get = AsyncMock(side_effect=_get)
    session.commit = AsyncMock()
    session.refresh = AsyncMock()

    async def _execute(_statement):
        result = MagicMock()
        result.scalars = MagicMock(return_value=list(media_rows.values()))
        return result

    session.execute = AsyncMock(side_effect=_execute)
    return session


CLIP_ID = "11111111-1111-1111-1111-111111111111"


def test_old_media_id_hits_the_right_slot_regardless_of_a_stale_media_index():
    """The regression this whole change exists to fix: media_index is stale
    (it names slot 0), but old_media_id names the item actually sitting at
    slot 1 — the id must win, and only that slot may change."""
    a, b, c, new = "media-a", "media-b", "media-c", "media-new"
    clip_row = _clip_row(CLIP_ID, {"images": [], "ai_videos": [a, b, c], "audios": []})
    session = _session_with(clip_row, {
        a: _media_row(a, "ai_video"),
        b: _media_row(b, "ai_video"),
        c: _media_row(c, "ai_video"),
        new: _media_row(new, "ai_video"),
    })

    body = SwapClipMediaBody(kind="ai_video", media_index=0, new_media_id=new, old_media_id=b)
    result = asyncio.run(clips_store.swap_clip_media(session, CLIP_ID, body))

    assert result is not None
    assert clip_row.media_refs["ai_videos"] == [a, new, c]
    session.commit.assert_awaited_once()


def test_old_media_id_not_in_bucket_raises_a_clear_error():
    a, b = "media-a", "media-b"
    clip_row = _clip_row(CLIP_ID, {"images": [], "ai_videos": [a], "audios": []})
    session = _session_with(clip_row, {a: _media_row(a, "ai_video"), b: _media_row(b, "ai_video")})

    body = SwapClipMediaBody(kind="ai_video", media_index=0, new_media_id=b, old_media_id="not-in-bucket")

    with pytest.raises(ValueError, match="old_media_id"):
        asyncio.run(clips_store.swap_clip_media(session, CLIP_ID, body))

    # Nothing was touched.
    assert clip_row.media_refs["ai_videos"] == [a]
    session.commit.assert_not_awaited()


def test_type_mismatch_rejected_on_the_id_based_path():
    a = "media-a"
    wrong_type = "media-wrong-type"
    clip_row = _clip_row(CLIP_ID, {"images": [], "ai_videos": [a], "audios": []})
    session = _session_with(clip_row, {
        a: _media_row(a, "ai_video"),
        wrong_type: _media_row(wrong_type, "image"),  # an image, not a video
    })

    body = SwapClipMediaBody(kind="ai_video", media_index=0, new_media_id=wrong_type, old_media_id=a)

    with pytest.raises(ValueError, match="type"):
        asyncio.run(clips_store.swap_clip_media(session, CLIP_ID, body))

    assert clip_row.media_refs["ai_videos"] == [a]
    session.commit.assert_not_awaited()


def test_type_mismatch_rejected_on_the_positional_fallback_path():
    """Validation is unconditional: the legacy positional path is not exempt
    just because it carries no old_media_id."""
    a = "media-a"
    wrong_type = "media-wrong-type"
    clip_row = _clip_row(CLIP_ID, {"images": [a], "ai_videos": [], "audios": []})
    session = _session_with(clip_row, {
        a: _media_row(a, "image"),
        wrong_type: _media_row(wrong_type, "ai_video"),  # a video, not an image
    })

    body = SwapClipMediaBody(kind="image", media_index=0, new_media_id=wrong_type)

    with pytest.raises(ValueError, match="type"):
        asyncio.run(clips_store.swap_clip_media(session, CLIP_ID, body))

    assert clip_row.media_refs["images"] == [a]
    session.commit.assert_not_awaited()


def test_positional_fallback_still_works_without_old_media_id():
    a, b = "media-a", "media-b"
    new = "media-new"
    clip_row = _clip_row(CLIP_ID, {"images": [], "ai_videos": [a, b], "audios": []})
    session = _session_with(clip_row, {
        a: _media_row(a, "ai_video"),
        b: _media_row(b, "ai_video"),
        new: _media_row(new, "ai_video"),
    })

    body = SwapClipMediaBody(kind="ai_video", media_index=1, new_media_id=new)
    result = asyncio.run(clips_store.swap_clip_media(session, CLIP_ID, body))

    assert result is not None
    assert clip_row.media_refs["ai_videos"] == [a, new]
    session.commit.assert_awaited_once()


def test_ai_video_kind_tolerates_the_legacy_video_type_value():
    """Generated videos are stored as type "ai_video" going forward but some
    rows predate that and still say "video" (see app/stores/media.py's own
    _TYPE_BUCKETS note) — the ai_video bucket must accept both."""
    a = "media-a"
    legacy = "media-legacy-video"
    clip_row = _clip_row(CLIP_ID, {"images": [], "ai_videos": [a], "audios": []})
    session = _session_with(clip_row, {
        a: _media_row(a, "ai_video"),
        legacy: _media_row(legacy, "video"),
    })

    body = SwapClipMediaBody(kind="ai_video", media_index=0, new_media_id=legacy)
    result = asyncio.run(clips_store.swap_clip_media(session, CLIP_ID, body))

    assert result is not None
    assert clip_row.media_refs["ai_videos"] == [legacy]


def test_media_index_out_of_range_still_raises_without_old_media_id():
    a = "media-a"
    clip_row = _clip_row(CLIP_ID, {"images": [], "ai_videos": [a], "audios": []})
    session = _session_with(clip_row, {a: _media_row(a, "ai_video")})

    body = SwapClipMediaBody(kind="ai_video", media_index=5, new_media_id=a)

    with pytest.raises(IndexError):
        asyncio.run(clips_store.swap_clip_media(session, CLIP_ID, body))
    session.commit.assert_not_awaited()


def test_new_media_id_missing_from_db_raises_lookup_error():
    a = "media-a"
    clip_row = _clip_row(CLIP_ID, {"images": [], "ai_videos": [a], "audios": []})
    session = _session_with(clip_row, {a: _media_row(a, "ai_video")})

    body = SwapClipMediaBody(kind="ai_video", media_index=0, new_media_id="does-not-exist")

    with pytest.raises(LookupError):
        asyncio.run(clips_store.swap_clip_media(session, CLIP_ID, body))
    session.commit.assert_not_awaited()
