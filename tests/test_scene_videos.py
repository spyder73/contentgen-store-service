"""Scene videos: per-user rows, the 500 cap, the internal status list (migration 0052).

Same harness as ``test_routines.py``.
"""
from __future__ import annotations

import asyncio
from typing import get_args

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db import get_session
from app.fastapi_app import create_fastapi_app
from app.models import SceneVideo
from app.schemas import ChatAction
from tests.test_chat import SECRET, USER_A, USER_B, Env, _consume_body, _create_tables, _h


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("INTERNAL_API_SECRET", SECRET)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'scene_videos.db'}", poolclass=NullPool)

    @event.listens_for(engine.sync_engine, "connect")
    def _enable_foreign_keys(dbapi_conn, _record):  # pragma: no cover - trivial
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async def create() -> None:
        await _create_tables(engine)
        async with engine.begin() as conn:
            await conn.run_sync(lambda c: SceneVideo.__table__.create(c))

    asyncio.run(create())
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _session():
        async with factory() as session:
            yield session

    app = create_fastapi_app()
    app.dependency_overrides[get_session] = _session
    with TestClient(app) as client:
        yield Env(client, factory)
    asyncio.run(engine.dispose())


def _mk(env: Env, user: str = USER_A, **body) -> dict:
    r = env.client.post(f"/v1/users/{user}/scene-videos", json=body, headers=_h(user))
    assert r.status_code == 200, r.text
    return r.json()


def _patch(env: Env, vid: str, user: str = USER_A, **body):
    return env.client.patch(f"/v1/users/{user}/scene-videos/{vid}", json=body, headers=_h(user))


def test_create_returns_a_draft_with_defaults(env):
    row = _mk(env, title="How a bill becomes law")
    assert row["status"] == "draft" and row["style_ref"] == "sceneStarter@1"
    assert row["user_id"] == USER_A and row["files"] is None and row["bundle"] is None
    got = env.client.get(f"/v1/users/{USER_A}/scene-videos/{row['id']}", headers=_h())
    assert got.status_code == 200 and got.json() == row


def test_another_users_video_is_404(env):
    row = _mk(env)
    for call in (env.client.get, env.client.patch):
        kw = {"json": {"title": "x"}} if call == env.client.patch else {}
        r = call(f"/v1/users/{USER_B}/scene-videos/{row['id']}", headers=_h(USER_B), **kw)
        assert r.status_code == 404
    assert env.client.get(f"/v1/users/{USER_B}/scene-videos", headers=_h(USER_B)).json() == []
    assert env.client.get(f"/v1/users/{USER_A}/scene-videos/not-a-uuid", headers=_h()).status_code == 404


def test_patch_moves_the_row_along_and_null_clears(env):
    vid = _mk(env)["id"]
    bundle = {"media_id": "m1", "sha256": "ab", "image_ref": "img"}
    r = _patch(env, vid, status="built", files={"a.tsx": "x"}, bundle=bundle, session_id="s1",
               woken_at="2026-11-21T10:00:00Z")
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["status"] == "built" and out["files"] == {"a.tsx": "x"} and out["bundle"] == bundle
    assert out["woken_at"].startswith("2026-11-21T10:00:00")
    cleared = _patch(env, vid, session_id=None, woken_at=None).json()
    assert cleared["session_id"] is None and cleared["woken_at"] is None
    assert cleared["status"] == "built" and cleared["bundle"] == bundle  # untouched fields stay


def test_patch_validation(env):
    vid = _mk(env)["id"]
    assert _patch(env, vid, files={f"f{i}.tsx": "x" for i in range(49)}).status_code == 422
    assert _patch(env, vid, files={f"f{i}.tsx": "x" for i in range(48)}).status_code == 200
    assert _patch(env, vid, status="weird").status_code == 422
    assert _patch(env, vid, status=None).status_code == 422
    assert _patch(env, vid, user_id=USER_B).status_code == 422
    assert _patch(env, vid, files={"a": "x" * (64 * 1024 + 1)}).status_code == 422
    assert _patch(env, vid, files={"a": "x" * (64 * 1024)}).status_code == 200
    nine = {f"f{i}": "x" * (64 * 1024) for i in range(9)}
    assert _patch(env, vid, files=nine).status_code == 422  # 576 KiB in all
    assert _patch(env, vid, files={f"f{i}": "x" * (64 * 1024) for i in range(8)}).status_code == 200  # exactly 512 KiB


def test_the_501st_create_is_409(env):
    async def fill() -> None:
        async with env.factory() as s:
            s.add_all(SceneVideo(id=f"00000000-0000-4000-8000-{i:012d}", user_id=USER_A) for i in range(500))
            await s.commit()

    asyncio.run(fill())
    r = env.client.post(f"/v1/users/{USER_A}/scene-videos", json={}, headers=_h())
    assert r.status_code == 409 and r.json()["detail"] == "scene_video_limit"
    assert env.client.post(f"/v1/users/{USER_B}/scene-videos", json={}, headers=_h(USER_B)).status_code == 200


def test_list_is_newest_first_and_capped_at_50(env):
    ids = [_mk(env, title=f"v{i}")["id"] for i in range(3)]
    assert [v["id"] for v in env.client.get(f"/v1/users/{USER_A}/scene-videos", headers=_h()).json()] == ids[::-1]
    assert len(env.client.get(f"/v1/users/{USER_A}/scene-videos?limit=2", headers=_h()).json()) == 2
    assert env.client.get(f"/v1/users/{USER_A}/scene-videos?limit=51", headers=_h()).status_code == 422


def test_internal_list_is_every_users_rendering_rows_behind_the_secret(env):
    a, b, c = _mk(env)["id"], _mk(env, USER_B)["id"], _mk(env)["id"]
    _patch(env, a, status="rendering")
    _patch(env, b, USER_B, status="rendering")
    _patch(env, c, status="ready")
    r = env.client.get("/v1/internal/scene-videos?status=rendering", headers=_h())
    assert r.status_code == 200
    assert {v["id"] for v in r.json()} == {a, b}
    assert env.client.get("/v1/internal/scene-videos?status=weird", headers=_h()).status_code == 422
    assert env.client.get("/v1/internal/scene-videos?status=rendering").status_code == 401


def test_the_action_literal_ends_with_the_scene_actions():
    assert get_args(ChatAction)[-2:] == ("scene.build", "scene.render")


@pytest.mark.parametrize("action", ["scene.build", "scene.render"])
def test_a_scene_approval_row_is_accepted(env, action):
    subject = "scene:abc|v1"
    thread_id, approval_id, token = env.approved(action=action, subject=subject, amount_usd=0)
    stored = env.client.get(f"/v1/chat/approvals/{approval_id}", headers=_h()).json()
    assert (stored["action"], stored["status"]) == (action, "approved")
    r = env.consume(_consume_body(thread_id, token, action=action, subject=subject, needed_usd=0))
    assert r.status_code == 200 and r.json()["status"] == "used"
