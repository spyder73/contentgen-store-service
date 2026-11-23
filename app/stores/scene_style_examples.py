"""Scene style examples: the shot plan of a rendered scene video, kept on its style (migration 0054).

Every function is scoped by ``user_id``. A put upserts by video and, in the same transaction,
keeps only the newest ``SCENE_EXAMPLES_KEEP`` per (user, style).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import SceneStyleExample
from ..schemas import SCENE_EXAMPLES_KEEP, SceneStyleExampleIn
from .scene_videos import SceneVideoError, _owned

LIST_MAX = 20


async def put(
    session: AsyncSession, user_id: str, video_id: str, payload: SceneStyleExampleIn
) -> SceneStyleExample:
    video = await _owned(session, user_id, video_id)
    if video is None:
        raise SceneVideoError(404, "scene_video_not_found")
    row = (
        await session.execute(select(SceneStyleExample).where(SceneStyleExample.video_id == video.id))
    ).scalar_one_or_none()
    if row is None:
        row = SceneStyleExample(id=str(uuid.uuid4()), user_id=user_id, video_id=video.id)
        session.add(row)
    row.style_id = payload.style_id
    row.style_ref = payload.style_ref
    row.title = payload.title
    row.topic = payload.topic
    row.plan = payload.plan
    row.updated_at = datetime.now(timezone.utc)  # an unchanged re-put still counts as the newest
    await session.flush()
    keep = (
        select(SceneStyleExample.id)
        .where(SceneStyleExample.user_id == user_id, SceneStyleExample.style_id == payload.style_id)
        .order_by(SceneStyleExample.updated_at.desc(), SceneStyleExample.id.desc())
        .limit(SCENE_EXAMPLES_KEEP)
    )
    await session.execute(
        delete(SceneStyleExample).where(
            SceneStyleExample.user_id == user_id,
            SceneStyleExample.style_id == payload.style_id,
            SceneStyleExample.id.not_in(keep),
        )
    )
    await session.commit()
    await session.refresh(row)
    return row


async def list_for_style(
    session: AsyncSession, user_id: str, style_id: str, limit: int = LIST_MAX
) -> list[SceneStyleExample]:
    limit = max(1, min(limit, LIST_MAX))
    return list(
        (
            await session.execute(
                select(SceneStyleExample)
                .where(SceneStyleExample.user_id == user_id, SceneStyleExample.style_id == style_id)
                .order_by(SceneStyleExample.updated_at.desc(), SceneStyleExample.id.desc())
                .limit(limit)
            )
        ).scalars()
    )
