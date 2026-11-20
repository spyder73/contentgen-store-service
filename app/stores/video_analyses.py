"""Video analyses: a long video's index and picks, kept 30 days (migration 0049).

Expiry is lazy, as no purge job exists: an expired row is absent on read (``None``), a save
replaces it in place and deletes the caller's other expired rows, and ``add_picks`` refuses it.
Every function is scoped by ``user_id``: another user's row is simply not found.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import VideoAnalysis
from ..schemas import VideoAnalysisIn, VideoAnalysisPicksIn

TTL = timedelta(days=30)
PICKS_KEEP = 20


async def get_by_video(
    session: AsyncSession, user_id: str, platform: str, video_id: str, *, now: datetime
) -> VideoAnalysis | None:
    return (
        await session.execute(
            select(VideoAnalysis)
            .where(
                VideoAnalysis.user_id == user_id,
                VideoAnalysis.platform == platform,
                VideoAnalysis.video_id == video_id,
                VideoAnalysis.expires_at > now,
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def save(
    session: AsyncSession,
    user_id: str,
    platform: str,
    video_id: str,
    payload: VideoAnalysisIn,
    *,
    now: datetime,
) -> VideoAnalysis:
    """Upsert: the row is replaced whole, ``picks`` back to ``[]`` and the 30 days restarted."""
    await session.execute(
        delete(VideoAnalysis).where(
            VideoAnalysis.user_id == user_id,
            VideoAnalysis.expires_at <= now,
            (VideoAnalysis.platform != platform) | (VideoAnalysis.video_id != video_id),
        )
    )
    row = (
        await session.execute(
            select(VideoAnalysis)
            .where(
                VideoAnalysis.user_id == user_id,
                VideoAnalysis.platform == platform,
                VideoAnalysis.video_id == video_id,
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if row is None:
        row = VideoAnalysis(id=str(uuid.uuid4()), user_id=user_id, platform=platform, video_id=video_id)
        session.add(row)
    row.url = payload.url
    row.title = payload.title
    row.channel = payload.channel
    row.duration_s = payload.duration_s
    row.transcript = payload.transcript
    row.signals = payload.signals
    row.candidates = payload.candidates
    row.picks = []
    row.created_at = now
    row.expires_at = now + TTL
    await session.commit()
    return row


async def add_picks(
    session: AsyncSession, user_id: str, analysis_id: str, payload: VideoAnalysisPicksIn, *, now: datetime
) -> VideoAnalysis | None:
    """Append one pick set and keep the newest 20; a missing, expired or foreign row is ``None``."""
    row = (
        await session.execute(
            select(VideoAnalysis)
            .where(
                VideoAnalysis.id == analysis_id,
                VideoAnalysis.user_id == user_id,
                VideoAnalysis.expires_at > now,
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    entry = {"brief": payload.brief, "created_at": now.isoformat(), "moments": payload.moments}
    row.picks = [*row.picks, entry][-PICKS_KEEP:]
    await session.commit()
    return row
