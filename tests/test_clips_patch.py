"""PATCH /v1/clips/{id}: partial clip update.

The Go backend now persists clip metadata edits (name/style/metadata/
media_refs/is_dirty) immediately, but the store previously offered only a
full-row PUT whose upsert assigns every column unconditionally — a
metadata-only save had to GET-then-PUT, and any transient error or race
with the render-completion write could wipe finished_at/thumbnail_url/
render_output_urls. ``patch_clip`` writes ONLY the keys present in the
request body: an absent key leaves its column untouched, an explicit
``null`` still clears it.

Exercises the store function directly with a mocked AsyncSession (same
pattern as tests/test_clips_swap.py) — no real Postgres needed — plus the
HTTP layer for the empty-body 400 and the exclude_unset semantics of
``ClipPromptPatch`` itself.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.db import get_session
from app.fastapi_app import create_fastapi_app
from app.models import ClipPrompt
from app.schemas import ClipPromptPatch
from app.stores import clips as clips_store

CLIP_ID = "11111111-1111-1111-1111-111111111111"


def _clip_row(**overrides) -> SimpleNamespace:
    now = datetime.now(timezone.utc)
    base = dict(
        id=CLIP_ID,
        name="original name",
        metadata_={"a": 1},
        style={"look": "cinematic"},
        media_refs={"images": ["m1"], "ai_videos": [], "audios": []},
        render_output_urls=["https://example.com/out.mp4"],
        is_dirty=False,
        finished_at=now,
        thumbnail_url="https://example.com/thumb.png",
        created_at=now,
        updated_at=now,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _session_with(clip_row: SimpleNamespace | None) -> MagicMock:
    session = MagicMock()

    async def _get(model, id_):
        if model is ClipPrompt and clip_row is not None and id_ == clip_row.id:
            return clip_row
        return None

    session.get = AsyncMock(side_effect=_get)
    session.commit = AsyncMock()
    session.refresh = AsyncMock()
    return session


def test_metadata_only_patch_leaves_render_columns_untouched():
    row = _clip_row()
    session = _session_with(row)

    result = asyncio.run(
        clips_store.patch_clip(session, CLIP_ID, {"name": "new name", "metadata": {"b": 2}})
    )

    assert result is not None
    assert row.name == "new name"
    assert row.metadata_ == {"b": 2}
    # Render-owned columns: never touched because they were absent from fields.
    assert row.finished_at is not None
    assert row.thumbnail_url == "https://example.com/thumb.png"
    assert row.render_output_urls == ["https://example.com/out.mp4"]
    session.commit.assert_awaited_once()


def test_explicit_null_clears_render_output_urls():
    row = _clip_row()
    session = _session_with(row)

    result = asyncio.run(clips_store.patch_clip(session, CLIP_ID, {"render_output_urls": None}))

    assert result is not None
    assert row.render_output_urls is None
    # Everything else, absent from fields, is untouched.
    assert row.name == "original name"
    assert row.finished_at is not None
    assert row.thumbnail_url == "https://example.com/thumb.png"


def test_absent_key_is_never_written_even_when_falsy_elsewhere():
    row = _clip_row(is_dirty=True)
    session = _session_with(row)

    asyncio.run(clips_store.patch_clip(session, CLIP_ID, {"name": "renamed"}))

    # is_dirty was never in the patch body -> stays True, not reset to the
    # ClipPromptIn/upsert_clip default of False.
    assert row.is_dirty is True


def test_foreign_or_missing_clip_returns_none():
    session = _session_with(None)

    result = asyncio.run(clips_store.patch_clip(session, "does-not-exist", {"name": "x"}))

    assert result is None
    session.commit.assert_not_awaited()


def test_clip_prompt_patch_exclude_unset_distinguishes_absent_from_null():
    body = ClipPromptPatch(**{"render_output_urls": None})
    fields = body.model_dump(exclude_unset=True)

    assert fields == {"render_output_urls": None}
    assert "name" not in fields
    assert "thumbnail_url" not in fields


async def _mock_session():
    yield MagicMock()


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_SECRET", "test-secret-xyz")
    app = create_fastapi_app()
    app.dependency_overrides[get_session] = _mock_session
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


def test_empty_body_returns_400(client):
    resp = client.patch(
        "/v1/clips/" + CLIP_ID,
        json={},
        headers={"X-Internal-Secret": "test-secret-xyz", "X-User-ID": "u1"},
    )
    assert resp.status_code == 400
