"""Which media a clip may use, and the one spelling its ids are kept in.

A clip's media is its owner's: its own rows plus rows with no owner. Reads of a
clip's media_refs (get_full_clip, swap) filter by the clip row's owner whether or
not the caller sends a header, and a write drops the ids the owner may not use.
Every media id is compared and stored in canonical form (lowercase, hyphenated),
however the caller typed it.
"""
from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import ClipPrompt, MediaItem
from ..schemas import ClipFullOut, ClipPromptOut, MediaItemOut
from ..uuids import canonical, uuid_form

BUCKETS = ("images", "ai_videos", "audios")


def canonical_refs(media_refs):
    """media_refs with each well-formed media id in canonical form."""
    if not isinstance(media_refs, dict):
        return media_refs
    refs = dict(media_refs)
    for bucket in BUCKETS:
        if isinstance(refs.get(bucket), list):
            refs[bucket] = [canonical(v) for v in refs[bucket]]
    return refs


def ref_ids(media_refs: dict | None) -> list[str]:
    """The canonical media ids a clip's refs name, in order. An id that is not a
    uuid cannot be a row (and would fail the uuid column's cast), so it is left out."""
    forms = (uuid_form(v) for b in BUCKETS for v in (media_refs or {}).get(b) or [])
    return [form for form in forms if form]


def media_owner(row: ClipPrompt | None, user_id: str | None) -> str | None:
    """Whose media a clip may use: its owner's, whoever asks and whether or not a
    header is sent. A clip with no owner (legacy), or none yet (a new row), falls
    back to the caller; with neither there is no one to filter by (an internal
    job on a legacy clip)."""
    return (row.user_id if row is not None else None) or user_id or None


def may_use(media_user_id: str | None, owner: str | None) -> bool:
    """The rule, once: a clip owned by ``owner`` may use media with no owner (system
    or legacy) and media that is the owner's. With no owner to filter by, any."""
    return owner is None or media_user_id in (None, owner)


def usable_media(owner: str):
    """``may_use`` as a WHERE clause."""
    return or_(MediaItem.user_id.is_(None), MediaItem.user_id == owner)


async def without_foreign_refs(
    session: AsyncSession, media_refs, row: ClipPrompt | None, user_id: str | None
):
    """media_refs, canonical, minus the ids of media the clip's owner may not use.

    The owner is the existing row's, else the caller (a new row is theirs). An id
    with no media row is kept: the backend writes a clip's refs while a placeholder
    item is still generating and its row not yet saved. Dropping silently, and
    answering the write from what was submitted, is what keeps a caller from
    telling a dropped id from a kept one by the write itself.
    """
    refs = canonical_refs(media_refs)
    owner = media_owner(row, user_id)
    ids = ref_ids(refs)
    if not owner or not ids:
        return refs
    result = await session.execute(
        select(MediaItem.id).where(MediaItem.id.in_(ids), ~usable_media(owner))
    )
    foreign = {str(i) for i in result.scalars()}
    if not foreign:
        return refs
    return {
        **refs,
        **{
            bucket: [v for v in refs[bucket] if uuid_form(v) not in foreign]
            for bucket in BUCKETS
            if isinstance(refs.get(bucket), list)
        },
    }


def is_foreign(row: ClipPrompt, user_id: str | None) -> bool:
    """True when ``row`` is owned by someone other than the caller.

    The by-id routes answer that exactly as a missing clip, so an id never
    reveals that it exists. A caller with no user (an internal job: the
    pipeline runner, the scheduler) is not filtered, and a legacy row with no
    owner (clip_prompts.user_id predates multi-tenancy, with no backfill) stays
    reachable by everyone, as it is today.
    """
    return bool(user_id) and row.user_id is not None and row.user_id != user_id


async def get_full_clip(
    session: AsyncSession, id: str, user_id: str | None = None
) -> ClipFullOut | None:
    # NOTE: deliberately no ORDER BY on the `id.in_(all_ids)` query below —
    # Postgres does not guarantee IN-list order, so a caller computing a
    # POSITIONAL media_index off this response's array can drift from
    # media_refs' own bucket order (this was a live hazard: swap_clip_media
    # was positional-only). Fixing the query's ordering was consciously
    # skipped here: it is a wide-blast-radius change (every consumer of this
    # endpoint), and SwapClipMediaBody.old_media_id (see clip_swap.py) removes
    # the load-bearing need for order to matter — a slot resolved BY ID cannot
    # be the wrong one regardless of what order this array comes back in.
    row = await session.get(ClipPrompt, id)
    if row is None or is_foreign(row, user_id):
        return None
    clip_out = ClipPromptOut.from_orm_row(row)
    media_refs = row.media_refs or {"images": [], "ai_videos": [], "audios": []}
    all_ids = ref_ids(media_refs)
    media_items: list[MediaItemOut] = []
    if all_ids:
        # Only media the clip's owner may use: a ref to someone else's media (or
        # to a row not saved) is simply not returned, whatever form it was
        # written in and whether or not the reader sends a header. A reader with
        # none (an internal path) must not become the way to someone's media.
        query = select(MediaItem).where(MediaItem.id.in_(all_ids))
        if owner := media_owner(row, user_id):
            query = query.where(usable_media(owner))
        result = await session.execute(query)
        media_items = [MediaItemOut.from_orm_row(m) for m in result.scalars()]
    return ClipFullOut(clip=clip_out, media=media_items)
