"""Video analyses: a long video's index and picks, kept 30 days (migration 0049).

Expiry is lazy, as no purge job exists: an expired row is absent on read (``None``), a save
replaces it in place and deletes the caller's other expired rows, and ``add_picks`` refuses it.
Every function is scoped by ``user_id``: another user's row is simply not found.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import VideoAnalysis
from ..schemas import VideoAnalysisIn, VideoAnalysisPicksIn

TTL = timedelta(days=30)
PICKS_KEEP = 20
MAX_LIVE_PER_USER = 200
# Byte caps on a field's compact JSON (field -> (bytes, label in the 413 detail)).
FIELD_CAPS = {
    "transcript": (4 * 1024 * 1024, "4 MiB"),
    "signals": (1024 * 1024, "1 MiB"),
    "candidates": (256 * 1024, "256 KiB"),
    "pick": (64 * 1024, "64 KiB"),
}
LIMIT_REACHED = "video_analysis_limit_reached"


class VideoAnalysisError(Exception):
    """A refusal with its HTTP status and a fixed detail."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _check_size(field: str, value: object) -> None:
    cap, label = FIELD_CAPS[field]
    if len(json.dumps(value, separators=(",", ":")).encode()) > cap:
        raise VideoAnalysisError(413, f"{field}_too_large: over {label}")


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


async def _find(session: AsyncSession, user_id: str, platform: str, video_id: str) -> VideoAnalysis | None:
    """The row for the video, expired or not."""
    return (
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


async def _write(
    session: AsyncSession,
    user_id: str,
    platform: str,
    video_id: str,
    payload: VideoAnalysisIn,
    now: datetime,
    row: VideoAnalysis | None,
) -> VideoAnalysis:
    await session.execute(
        delete(VideoAnalysis).where(
            VideoAnalysis.user_id == user_id,
            VideoAnalysis.expires_at <= now,
            (VideoAnalysis.platform != platform) | (VideoAnalysis.video_id != video_id),
        ).execution_options(synchronize_session=False)
    )
    if row is None:
        # Only a save that creates a live row counts against the cap; replacing a live one never does.
        live = (
            await session.execute(
                select(func.count())
                .select_from(VideoAnalysis)
                .where(VideoAnalysis.user_id == user_id, VideoAnalysis.expires_at > now)
            )
        ).scalar_one()
        if live >= MAX_LIVE_PER_USER:
            raise VideoAnalysisError(409, LIMIT_REACHED)
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


async def save(
    session: AsyncSession,
    user_id: str,
    platform: str,
    video_id: str,
    payload: VideoAnalysisIn,
    *,
    now: datetime,
) -> VideoAnalysis:
    """Upsert: the row is replaced whole, ``picks`` back to ``[]`` and the 30 days restarted.

    A concurrent first save of the same video loses the unique key at commit; it then rolls
    back, re-reads the winner's row and updates that, so neither caller sees an error."""
    if payload.transcript is not None:
        _check_size("transcript", payload.transcript)
    _check_size("signals", payload.signals)
    _check_size("candidates", payload.candidates)
    row = await _find(session, user_id, platform, video_id)
    try:
        return await _write(session, user_id, platform, video_id, payload, now, row)
    except IntegrityError:
        await session.rollback()
        row = await _find(session, user_id, platform, video_id)
        if row is None:
            raise
        return await _write(session, user_id, platform, video_id, payload, now, row)


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
            .with_for_update()  # serialises concurrent appends on Postgres; sqlite ignores it
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    entry = {"brief": payload.brief, "created_at": now.isoformat(), "moments": payload.moments}
    _check_size("pick", entry)
    row.picks = [*row.picks, entry][-PICKS_KEEP:]
    await session.commit()
    return row
