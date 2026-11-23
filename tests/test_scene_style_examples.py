"""Scene style examples: the upsert, the newest-20 cap, the caps, the owner scope (migration 0054).

Same harness as ``test_scene_videos.py``.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateTable

from app.db import get_session
from app.fastapi_app import create_fastapi_app
from app.models import ClipPrompt, MediaItem, RenderTemplate, SceneStyleExample, SceneVideo
from tests.test_chat import SECRET, USER_A, USER_B, Env, _create_tables, _h


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("INTERNAL_API_SECRET", SECRET)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'scene_examples.db'}", poolclass=NullPool)

    @event.listens_for(engine.sync_engine, "connect")
    def _enable_foreign_keys(dbapi_conn, _record):  # pragma: no cover - trivial
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async def create() -> None:
        await _create_tables(engine)
        async with engine.begin() as conn:
            for table in (RenderTemplate, ClipPrompt, MediaItem):
                ddl = str(CreateTable(table.__table__).compile(dialect=conn.dialect))
                await conn.exec_driver_sql(ddl.replace("'{}'::jsonb", "'{}'"))
            await conn.run_sync(lambda c: SceneVideo.__table__.create(c))
            await conn.run_sync(lambda c: SceneStyleExample.__table__.create(c))

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


def _video(env: Env, user: str = USER_A) -> str:
    r = env.client.post(f"/v1/users/{user}/scene-videos", json={}, headers=_h(user))
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _body(**over) -> dict:
    body = {"style_id": "style-1", "style_ref": "sceneStarter@1", "title": "T", "topic": "A topic",
            "plan": {"shots": [{"component": "title"}]}}
    return {**body, **over}


def _put(env: Env, vid: str, user: str = USER_A, **over):
    return env.client.put(f"/v1/users/{user}/scene-style-examples/{vid}", json=_body(**over), headers=_h(user))


def _list(env: Env, user: str = USER_A, **params):
    return env.client.get(f"/v1/users/{user}/scene-style-examples", params=params, headers=_h(user))


def test_put_creates_and_a_second_put_updates_the_one_row(env):
    vid = _video(env)
    first = _put(env, vid)
    assert first.status_code == 200, first.text
    row = first.json()
    assert set(row) == {"id", "video_id", "style_id", "style_ref", "title", "topic", "plan", "created_at", "updated_at"}
    assert row["video_id"] == vid and row["plan"] == {"shots": [{"component": "title"}]}
    second = _put(env, vid, title="New", plan={"shots": []}).json()
    assert second["id"] == row["id"] and second["title"] == "New" and second["plan"] == {"shots": []}
    assert second["created_at"] == row["created_at"] and second["updated_at"] > row["updated_at"]
    assert len(_list(env, style_id="style-1").json()) == 1


def test_the_21st_example_evicts_the_oldest_and_leaves_other_styles(env):
    other = _video(env)
    assert _put(env, other, style_id="style-2").status_code == 200
    vids = [_video(env) for _ in range(21)]
    for vid in vids:
        assert _put(env, vid).status_code == 200
    rows = _list(env, style_id="style-1").json()
    assert len(rows) == 20
    assert [r["video_id"] for r in rows] == vids[:0:-1]  # newest first; the first video was evicted
    assert [r["video_id"] for r in _list(env, style_id="style-2").json()] == [other]


def test_a_put_for_another_users_video_is_404(env):
    vid = _video(env)
    r = _put(env, vid, user=USER_B)
    assert r.status_code == 404 and r.json()["detail"] == "scene_video_not_found"
    assert _put(env, "not-a-uuid").status_code == 404
    assert _list(env, user=USER_B, style_id="style-1").json() == []


def test_the_caps_are_422(env):
    vid = _video(env)
    big = {"k": "x" * 65536}
    for over in (
        {"style_id": ""}, {"style_id": "s" * 65}, {"style_ref": "r" * 81}, {"title": "t" * 201},
        {"topic": "x" * 4001}, {"plan": []}, {"plan": big}, {"plan": None}, {"extra": 1},
    ):
        assert _put(env, vid, **over).status_code == 422, over
    for over in ({"style_id": "s" * 64}, {"style_ref": "r" * 80}, {"title": "t" * 200}, {"topic": "x" * 4000}):
        assert _put(env, vid, **over).status_code == 200, over
    assert _put(env, vid, plan={"k": "x" * (65536 - len('{"k":""}'))}).status_code == 200


def test_get_filters_by_user_and_style_and_orders_by_recency(env):
    a, b, c = _video(env), _video(env), _video(env)
    _put(env, a)
    _put(env, b)
    _put(env, c, style_id="style-2")
    _put(env, a, title="touched")  # a is now the newest of style-1
    other = _video(env, USER_B)
    _put(env, other, user=USER_B)
    assert [r["video_id"] for r in _list(env, style_id="style-1").json()] == [a, b]
    assert [r["video_id"] for r in _list(env, style_id="style-1", limit=1).json()] == [a]
    assert [r["video_id"] for r in _list(env, style_id="style-2").json()] == [c]
    assert _list(env, style_id="nope").json() == []
    assert _list(env, user=USER_B, style_id="style-1").json()[0]["video_id"] == other
    for params in ({}, {"style_id": "s", "limit": 0}, {"style_id": "s", "limit": 21}):
        assert _list(env, **params).status_code == 422


def test_deleting_the_video_deletes_its_example(env):
    vid = _video(env)
    _put(env, vid)

    async def drop() -> int:
        async with env.factory() as s:
            await s.delete(await s.get(SceneVideo, vid))
            await s.commit()
            return len((await s.execute(select(SceneStyleExample))).scalars().all())

    assert asyncio.run(drop()) == 0
