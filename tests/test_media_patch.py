"""Atomic media PATCH contract used by background generator persistence."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy.dialects import postgresql

from app.schemas import MediaItemPatch
from app.stores import media as media_store


def _row(media_id: str, user_id: str):
    now = datetime.now(timezone.utc)
    return SimpleNamespace(
        id=media_id,
        user_id=user_id,
        clip_id=None,
        type="image",
        prompt="prompt",
        file_url=f"/media/uploads/{media_id}.png",
        metadata_={"cost_credits": 1.25, "persistence_status": "ready"},
        output_spec=None,
        is_favourite=False,
        name="",
        pipeline_run_id=None,
        scene_id=None,
        parent_media_id=None,
        role=None,
        thumbnail_content_type="image/webp",
        file_mime_type="image/png",
        micro_thumbnail=None,
        created_at=now,
        updated_at=now,
    )


def test_patch_media_compiles_to_one_owner_scoped_jsonb_merge_update():
    media_id = "11111111-1111-1111-1111-111111111111"
    user_id = "user-1"
    session = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = media_id
    session.execute = AsyncMock(return_value=result)
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    session.get = AsyncMock(return_value=_row(media_id, user_id))

    response = asyncio.run(media_store.patch_media(
        session,
        media_id,
        MediaItemPatch(
            file_url=f"/media/uploads/{media_id}.png",
            metadata_merge={"persistence_status": "ready"},
        ),
        user_id=user_id,
    ))

    assert response is not None and response.metadata["cost_credits"] == 1.25
    statement = session.execute.await_args.args[0]
    sql = str(statement.compile(dialect=postgresql.dialect()))
    assert "UPDATE media_items" in sql
    assert "metadata" in sql and "||" in sql
    assert "user_id" in sql
    session.commit.assert_awaited_once()
    session.rollback.assert_not_awaited()


def test_patch_media_rolls_back_and_returns_none_when_row_is_not_owned():
    session = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(return_value=result)
    session.commit = AsyncMock()
    session.rollback = AsyncMock()

    response = asyncio.run(media_store.patch_media(
        session,
        "missing",
        MediaItemPatch(metadata_merge={"persistence_status": "ready"}),
        user_id="user-1",
    ))

    assert response is None
    session.rollback.assert_awaited_once()
    session.commit.assert_not_awaited()
