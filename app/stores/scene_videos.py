"""Scene videos: the code-made 3D explainer videos the chat agent makes (migration 0052).

Every per-user function is scoped by ``user_id`` (another user's row is simply not found).
``by_status`` is cross-user: the route that calls it (boot recovery) sits behind the internal
secret only.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import SceneVideo
from ..schemas import SCENE_VIDEO_LIMIT, SceneVideoIn, SceneVideoPatch
from .chat import _owned_thread
from .credits import _lock_user_row
from .media import _get_owned

LIST_MAX = 50
BY_STATUS_MAX = 500


class SceneVideoError(Exception):
    """A refusal with its HTTP status and a fixed detail."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


async def _owned(session: AsyncSession, user_id: str, video_id: str) -> SceneVideo | None:
    try:
        vid = str(uuid.UUID(video_id))
    except (ValueError, AttributeError, TypeError):
        return None
    return (
        await session.execute(select(SceneVideo).where(SceneVideo.id == vid, SceneVideo.user_id == user_id))
    ).scalar_one_or_none()


async def _check_references(
    session: AsyncSession, user_id: str, thread_id: str | None, media_ids: list[str | None]
) -> None:
    """Every referenced chat thread and media row must be the user's own (else 404 ``not_found``)."""
    if thread_id is not None and await _owned_thread(session, user_id, thread_id) is None:
        raise SceneVideoError(404, "not_found")
    for media_id in media_ids:
        if media_id is None:
            continue
        try:
            mid = str(uuid.UUID(media_id))
        except (ValueError, AttributeError, TypeError):
            raise SceneVideoError(404, "not_found")
        if await _get_owned(session, mid, user_id) is None:
            raise SceneVideoError(404, "not_found")


async def create(session: AsyncSession, user_id: str, payload: SceneVideoIn) -> SceneVideo:
    await _check_references(session, user_id, payload.thread_id, [])
    await _lock_user_row(session, user_id)
    held = (
        await session.execute(select(func.count()).select_from(SceneVideo).where(SceneVideo.user_id == user_id))
    ).scalar_one()
    if held >= SCENE_VIDEO_LIMIT:
        raise SceneVideoError(409, "scene_video_limit")
    row = SceneVideo(
        id=str(uuid.uuid4()),
        user_id=user_id,
        thread_id=payload.thread_id,
        style_ref=payload.style_ref,
        title=payload.title,
        status="draft",
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def list_for_user(session: AsyncSession, user_id: str, limit: int = LIST_MAX) -> list[SceneVideo]:
    limit = max(1, min(limit, LIST_MAX))
    return list(
        (
            await session.execute(
                select(SceneVideo)
                .where(SceneVideo.user_id == user_id)
                .order_by(SceneVideo.created_at.desc(), SceneVideo.id.desc())
                .limit(limit)
            )
        ).scalars()
    )


async def get(session: AsyncSession, user_id: str, video_id: str) -> SceneVideo | None:
    return await _owned(session, user_id, video_id)


async def patch(session: AsyncSession, user_id: str, video_id: str, payload: SceneVideoPatch) -> SceneVideo | None:
    row = await _owned(session, user_id, video_id)
    if row is None:
        return None
    sent = payload.model_fields_set
    bundle = payload.bundle if "bundle" in sent else None
    bundle_media = bundle.get("media_id") if bundle else None
    await _check_references(
        session,
        user_id,
        payload.thread_id if "thread_id" in sent else None,
        [
            payload.narration_media_id if "narration_media_id" in sent else None,
            payload.final_media_id if "final_media_id" in sent else None,
            bundle_media if bundle_media is not None else None,
        ],
    )
    for name in sent - {"clear"}:
        value = getattr(payload, name)
        if name == "voice_takes" and value is not None:
            value = [t.model_dump(mode="json") for t in value]
        setattr(row, name, value)
    for name in payload.clear:
        setattr(row, name, None)
    await session.commit()
    await session.refresh(row)
    return row


async def by_status(
    session: AsyncSession,
    status: str,
    *,
    unwoken: bool = False,
    since: datetime | None = None,
) -> list[SceneVideo]:
    """Cross-user rows of one status. Without filters: the oldest BY_STATUS_MAX (the rendering
    sweep). With ``unwoken`` (woken_at is null) or ``since`` (updated_at >= since): the newest
    BY_STATUS_MAX, so a boot pass sees recent rows however many old ones exist."""
    stmt = select(SceneVideo).where(SceneVideo.status == status)
    if not unwoken and since is None:
        stmt = stmt.order_by(SceneVideo.created_at, SceneVideo.id)
    else:
        if unwoken:
            stmt = stmt.where(SceneVideo.woken_at.is_(None))
        if since is not None:
            stmt = stmt.where(SceneVideo.updated_at >= since)
        stmt = stmt.order_by(SceneVideo.updated_at.desc(), SceneVideo.id)
    return list((await session.execute(stmt.limit(BY_STATUS_MAX))).scalars())