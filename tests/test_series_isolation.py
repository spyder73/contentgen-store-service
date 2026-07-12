"""Cross-tenant isolation for series / characters / episodes / voice-snippets.

Series list/upsert are already user-scoped, but single-item get/delete used to
resolve purely by primary key with no owner predicate — a cross-tenant IDOR
letting any authenticated user read or permanently delete another user's series
(cascading to its characters/episodes/voice snippets). These tests pin two
things:

  * the store DELETEs are owner-scoped (the destructive path), and refuse to run
    without a user_id, and
  * the HTTP routes forward X-User-ID into the store and 401 without it.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

INTERNAL_SECRET = "test-secret-xyz"
os.environ["INTERNAL_API_SECRET"] = INTERNAL_SECRET

from app.db import get_session
from app.fastapi_app import create_fastapi_app
from app.schemas import CharacterOut, EpisodeOut, SeriesOut, VoiceSnippetOut
from app.stores import characters, episodes, series, voice_snippets


async def _mock_session():
    yield MagicMock()


@pytest.fixture(autouse=True)
def _keep_secret(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_SECRET", INTERNAL_SECRET)


@pytest.fixture()
def client():
    app = create_fastapi_app()
    app.dependency_overrides[get_session] = _mock_session
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


def _headers(user_id: str) -> dict[str, str]:
    return {"X-User-ID": user_id, "X-Internal-Secret": INTERNAL_SECRET}


def _no_user_headers() -> dict[str, str]:
    return {"X-Internal-Secret": INTERNAL_SECRET}


# ── store-level: the destructive DELETE must be owner-scoped ──────────────────


class _Capture:
    """Async session double that records the statement passed to execute()."""

    def __init__(self) -> None:
        self.statement = None

    async def execute(self, statement):
        self.statement = statement
        result = MagicMock()
        result.rowcount = 0
        return result

    async def commit(self):
        return None


@pytest.mark.asyncio
class TestDeleteScoping:
    async def test_delete_series_is_owner_scoped(self):
        sess = _Capture()
        uid = str(uuid.uuid4())
        await series.delete_series(sess, str(uuid.uuid4()), user_id=uid)
        sql = str(sess.statement)
        assert "series.user_id" in sql
        assert "series.id" in sql

    async def test_delete_character_is_owner_scoped(self):
        sess = _Capture()
        uid = str(uuid.uuid4())
        await characters.delete_character(sess, str(uuid.uuid4()), user_id=uid)
        sql = str(sess.statement)
        assert "characters.series_id" in sql
        assert "series.user_id" in sql

    async def test_delete_episode_is_owner_scoped(self):
        sess = _Capture()
        uid = str(uuid.uuid4())
        await episodes.delete_episode(sess, str(uuid.uuid4()), user_id=uid)
        sql = str(sess.statement)
        assert "episodes.series_id" in sql
        assert "series.user_id" in sql

    async def test_delete_voice_snippet_is_owner_scoped(self):
        sess = _Capture()
        uid = str(uuid.uuid4())
        await voice_snippets.delete_voice_snippet(sess, str(uuid.uuid4()), user_id=uid)
        sql = str(sess.statement)
        assert "voice_snippets.character_id" in sql
        assert "series.user_id" in sql


@pytest.mark.asyncio
class TestDeleteRequiresUserId:
    async def test_delete_series_requires_user_id(self):
        with pytest.raises(ValueError):
            await series.delete_series(MagicMock(), str(uuid.uuid4()), user_id=None)

    async def test_delete_character_requires_user_id(self):
        with pytest.raises(ValueError):
            await characters.delete_character(MagicMock(), str(uuid.uuid4()), user_id=None)

    async def test_delete_episode_requires_user_id(self):
        with pytest.raises(ValueError):
            await episodes.delete_episode(MagicMock(), str(uuid.uuid4()), user_id=None)

    async def test_delete_voice_snippet_requires_user_id(self):
        with pytest.raises(ValueError):
            await voice_snippets.delete_voice_snippet(MagicMock(), str(uuid.uuid4()), user_id=None)


# ── route-level: handlers forward X-User-ID and 401 without it ────────────────


def _series_out(id_: str) -> SeriesOut:
    now = datetime.now(timezone.utc)
    return SeriesOut(
        id=id_, name="n", description="", concept="", metadata={},
        created_at=now, updated_at=now,
    )


def _character_out(id_: str) -> CharacterOut:
    now = datetime.now(timezone.utc)
    return CharacterOut(
        id=id_, series_id=str(uuid.uuid4()), name="n", description="", voice="",
        reference_image_media_id=None, generator_profile_id=None, metadata={},
        created_at=now, updated_at=now,
    )


def _episode_out(id_: str) -> EpisodeOut:
    now = datetime.now(timezone.utc)
    return EpisodeOut(
        id=id_, series_id=str(uuid.uuid4()), episode_number=1, title="", synopsis="",
        prev_episode_summary="", metadata={}, created_at=now, updated_at=now,
    )


def _voice_out(id_: str) -> VoiceSnippetOut:
    now = datetime.now(timezone.utc)
    return VoiceSnippetOut(
        id=id_, character_id=str(uuid.uuid4()), file_url="", duration=0.0,
        metadata={}, created_at=now, updated_at=now,
    )


class TestSeriesRouteForwarding:
    def test_get_forwards_user_id(self, client):
        uid, sid = str(uuid.uuid4()), str(uuid.uuid4())
        with patch("app.stores.series.get_series", new=AsyncMock(return_value=_series_out(sid))) as m:
            resp = client.get(f"/v1/series/{sid}", headers=_headers(uid))
        assert resp.status_code == 200
        assert m.call_args.kwargs.get("user_id") == uid

    def test_get_without_user_id_401(self, client):
        resp = client.get(f"/v1/series/{uuid.uuid4()}", headers=_no_user_headers())
        assert resp.status_code == 401

    def test_get_404_when_not_owned(self, client):
        uid = str(uuid.uuid4())
        with patch("app.stores.series.get_series", new=AsyncMock(return_value=None)):
            resp = client.get(f"/v1/series/{uuid.uuid4()}", headers=_headers(uid))
        assert resp.status_code == 404

    def test_delete_forwards_user_id(self, client):
        uid, sid = str(uuid.uuid4()), str(uuid.uuid4())
        with patch("app.stores.series.delete_series", new=AsyncMock(return_value=True)) as m:
            resp = client.delete(f"/v1/series/{sid}", headers=_headers(uid))
        assert resp.status_code == 204
        assert m.call_args.kwargs.get("user_id") == uid

    def test_delete_without_user_id_401(self, client):
        resp = client.delete(f"/v1/series/{uuid.uuid4()}", headers=_no_user_headers())
        assert resp.status_code == 401

    def test_delete_404_when_not_owned(self, client):
        uid = str(uuid.uuid4())
        with patch("app.stores.series.delete_series", new=AsyncMock(return_value=False)):
            resp = client.delete(f"/v1/series/{uuid.uuid4()}", headers=_headers(uid))
        assert resp.status_code == 404


class TestCharacterRouteForwarding:
    def test_get_forwards_user_id(self, client):
        uid, cid = str(uuid.uuid4()), str(uuid.uuid4())
        with patch("app.stores.characters.get_character", new=AsyncMock(return_value=_character_out(cid))) as m:
            resp = client.get(f"/v1/characters/{cid}", headers=_headers(uid))
        assert resp.status_code == 200
        assert m.call_args.kwargs.get("user_id") == uid

    def test_get_without_user_id_401(self, client):
        resp = client.get(f"/v1/characters/{uuid.uuid4()}", headers=_no_user_headers())
        assert resp.status_code == 401

    def test_delete_forwards_user_id(self, client):
        uid, cid = str(uuid.uuid4()), str(uuid.uuid4())
        with patch("app.stores.characters.delete_character", new=AsyncMock(return_value=True)) as m:
            resp = client.delete(f"/v1/characters/{cid}", headers=_headers(uid))
        assert resp.status_code == 204
        assert m.call_args.kwargs.get("user_id") == uid

    def test_delete_without_user_id_401(self, client):
        resp = client.delete(f"/v1/characters/{uuid.uuid4()}", headers=_no_user_headers())
        assert resp.status_code == 401


class TestEpisodeRouteForwarding:
    def test_get_forwards_user_id(self, client):
        uid, eid = str(uuid.uuid4()), str(uuid.uuid4())
        with patch("app.stores.episodes.get_episode", new=AsyncMock(return_value=_episode_out(eid))) as m:
            resp = client.get(f"/v1/episodes/{eid}", headers=_headers(uid))
        assert resp.status_code == 200
        assert m.call_args.kwargs.get("user_id") == uid

    def test_get_without_user_id_401(self, client):
        resp = client.get(f"/v1/episodes/{uuid.uuid4()}", headers=_no_user_headers())
        assert resp.status_code == 401

    def test_delete_forwards_user_id(self, client):
        uid, eid = str(uuid.uuid4()), str(uuid.uuid4())
        with patch("app.stores.episodes.delete_episode", new=AsyncMock(return_value=True)) as m:
            resp = client.delete(f"/v1/episodes/{eid}", headers=_headers(uid))
        assert resp.status_code == 204
        assert m.call_args.kwargs.get("user_id") == uid

    def test_delete_without_user_id_401(self, client):
        resp = client.delete(f"/v1/episodes/{uuid.uuid4()}", headers=_no_user_headers())
        assert resp.status_code == 401


class TestVoiceSnippetRouteForwarding:
    def test_get_forwards_user_id(self, client):
        uid, vid = str(uuid.uuid4()), str(uuid.uuid4())
        with patch("app.stores.voice_snippets.get_voice_snippet", new=AsyncMock(return_value=_voice_out(vid))) as m:
            resp = client.get(f"/v1/voice-snippets/{vid}", headers=_headers(uid))
        assert resp.status_code == 200
        assert m.call_args.kwargs.get("user_id") == uid

    def test_get_without_user_id_401(self, client):
        resp = client.get(f"/v1/voice-snippets/{uuid.uuid4()}", headers=_no_user_headers())
        assert resp.status_code == 401

    def test_delete_forwards_user_id(self, client):
        uid, vid = str(uuid.uuid4()), str(uuid.uuid4())
        with patch("app.stores.voice_snippets.delete_voice_snippet", new=AsyncMock(return_value=True)) as m:
            resp = client.delete(f"/v1/voice-snippets/{vid}", headers=_headers(uid))
        assert resp.status_code == 204
        assert m.call_args.kwargs.get("user_id") == uid

    def test_delete_without_user_id_401(self, client):
        resp = client.delete(f"/v1/voice-snippets/{uuid.uuid4()}", headers=_no_user_headers())
        assert resp.status_code == 401
