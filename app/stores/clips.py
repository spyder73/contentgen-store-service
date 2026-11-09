from __future__ import annotations

from sqlalchemy import delete, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import ClipPrompt
from ..schemas import ClipPromptIn, ClipPromptOut, ClipSummaryOut, PagedResponse

# get_full_clip and swap_clip_media live beside the media rules they apply
# (clip_media.py, clip_swap.py) and are re-exported: callers use clips.<name>.
from .clip_media import canonical_refs, get_full_clip, is_foreign, without_foreign_refs
from .clip_swap import swap_clip_media

__all__ = [
    "delete_clip", "get_clip", "get_full_clip", "list_clip_summaries", "list_clips",
    "patch_clip", "swap_clip_media", "upsert_clip",
]


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


async def get_clip(
    session: AsyncSession, id: str, user_id: str | None = None
) -> ClipPromptOut | None:
    row = await session.get(ClipPrompt, id)
    if row is None or is_foreign(row, user_id):
        return None
    return ClipPromptOut.from_orm_row(row)


async def upsert_clip(
    session: AsyncSession, body: ClipPromptIn, user_id: str | None = None
) -> ClipPromptOut | None:
    row = await session.get(ClipPrompt, body.id)
    # The id is the caller's choice, so an upsert must not overwrite a row it
    # does not own: another user's clip is not found, and nothing is written.
    if row is not None and is_foreign(row, user_id):
        return None
    refs = await without_foreign_refs(session, body.media_refs, row, user_id)
    if row is None:
        row = ClipPrompt(id=body.id)
        if user_id:
            row.user_id = user_id
        session.add(row)
    row.name = body.name
    row.metadata_ = body.metadata
    row.style = body.style
    row.media_refs = refs
    row.render_output_urls = body.render_output_urls
    row.is_dirty = body.is_dirty
    row.finished_at = body.finished_at
    row.thumbnail_url = body.thumbnail_url
    await session.commit()
    await session.refresh(row)
    out = ClipPromptOut.from_orm_row(row)
    out.media_refs = canonical_refs(body.media_refs)  # as submitted, not as kept
    return out


async def patch_clip(
    session: AsyncSession, id: str, fields: dict, user_id: str | None = None
) -> ClipPromptOut | None:
    """Write only the keys present in ``fields`` (from ``model_dump(exclude_unset=True)``
    on ``ClipPromptPatch``). Unlike ``upsert_clip``, an omitted key leaves its column
    untouched — that is the whole point: a metadata-only patch must never default
    finished_at/thumbnail_url/render_output_urls back to None/[] the way the PUT's
    unconditional upsert does. ``metadata`` is remapped to the ORM's ``metadata_``
    attribute (the model column is named "metadata" in Postgres but "metadata" is a
    reserved attribute name on the Declarative base).
    """
    row = await session.get(ClipPrompt, id)
    if row is None or is_foreign(row, user_id):
        return None
    submitted = fields.get("media_refs")
    if "media_refs" in fields:
        kept = await without_foreign_refs(session, submitted, row, user_id)
        fields = {**fields, "media_refs": kept}
    for key, value in fields.items():
        attr = "metadata_" if key == "metadata" else key
        setattr(row, attr, value)
    await session.commit()
    await session.refresh(row)
    out = ClipPromptOut.from_orm_row(row)
    if "media_refs" in fields and submitted is not None:
        out.media_refs = canonical_refs(submitted)  # as submitted, not as kept
    return out


async def delete_clip(session: AsyncSession, id: str, user_id: str | None = None) -> bool:
    stmt = delete(ClipPrompt).where(ClipPrompt.id == id)
    if user_id:
        # In the statement, not after a read: another user's row is not matched.
        stmt = stmt.where(or_(ClipPrompt.user_id.is_(None), ClipPrompt.user_id == user_id))
    result = await session.execute(stmt)
    await session.commit()
    return result.rowcount > 0
