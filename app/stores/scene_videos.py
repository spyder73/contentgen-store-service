"""Scene videos: the code-made 3D explainer videos the chat agent makes (migration 0052).

Every per-user function is scoped by ``user_id`` (another user's row is simply not found).
``by_status`` is cross-user: the route that calls it (boot recovery) sits behind the internal
secret only.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import SceneVideo
from ..schemas import SCENE_VIDEO_LIMIT, SceneVideoIn, SceneVideoPatch
from .credits import _lock_user_row

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


async def create(session: AsyncSession, user_id: str, payload: SceneVideoIn) -> SceneVideo:
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
    for name in payload.model_fields_set:
        setattr(row, name, getattr(payload, name))
    await session.commit()
    await session.refresh(row)
    return row


async def by_status(session: AsyncSession, status: str) -> list[SceneVideo]:
    return list(
        (
            await session.execute(
                select(SceneVideo)
                .where(SceneVideo.status == status)
                .order_by(SceneVideo.created_at, SceneVideo.id)
                .limit(BY_STATUS_MAX)
            )
        ).scalars()
    )
