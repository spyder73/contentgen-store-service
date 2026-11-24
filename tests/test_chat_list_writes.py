"""What the list's stored facts follow: a thread's last line, its newest picture, a routine's run count."""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db import get_session
from app.fastapi_app import create_fastapi_app
from app.models import ChatThread, MediaItem, Routine, RoutineSpendDay
from tests.test_chat import SECRET, USER_A, USER_B, Env, _create_tables, _h
from tests.test_chat_productions import _new, _patch

AT = datetime(2026, 11, 24, 9, 0, tzinfo=timezone.utc)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """The chat harness plus the routine and media tables."""
    monkeypatch.setenv("INTERNAL_API_SECRET", SECRET)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'list.db'}", poolclass=NullPool)

    async def create() -> None:
        await _create_tables(engine)
        async with engine.begin() as conn:
            for table in (Routine.__table__, RoutineSpendDay.__table__, MediaItem.__table__):
                await conn.run_sync(lambda c, t=table: t.create(c))

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


def _thread_row(env: Env, thread_id: str) -> ChatThread:
    async def q(s):
        return (await s.execute(select(ChatThread).where(ChatThread.id == thread_id))).scalar_one()

    return env.db(q)


def _last(env: Env, thread_id: str) -> tuple[str, str, str]:
    t = _thread_row(env, thread_id)
    return (t.last_role, t.last_kind, t.last_text)


def _media(env: Env, kind: str, user: str = USER_A) -> str:
    media_id = str(uuid.uuid4())

    async def q(s):
        s.add(MediaItem(id=media_id, user_id=user, type=kind))
        await s.commit()

    env.db(q)
    return media_id


# ── the last line ────────────────────────────────────────────────────────────


def test_a_new_thread_has_no_last_line(env):
    assert _last(env, env.thread()["id"]) == ("", "", "")


def test_a_user_text_becomes_the_last_line(env):
    tid = env.thread()["id"]
    env.message(tid, text="Make **ten** cat pictures")
    assert _last(env, tid) == ("user", "text", "Make ten cat pictures")


@pytest.mark.parametrize("kind", ["tool", "run", "node"])
def test_a_step_leaves_the_last_line_as_it_was(env, kind):
    tid = env.thread()["id"]
    env.message(tid, text="first")
    env.message(tid, role="operator", kind=kind, text="step output")
    assert _last(env, tid) == ("user", "text", "first")


def test_media_without_text_or_a_bare_fence_leaves_it_as_it_was(env):
    tid = env.thread()["id"]
    env.message(tid, role="operator", text="Here you go")
    env.message(tid, role="operator", kind="media", text="")
    env.message(tid, role="operator", text="```\nonly code\n```")
    assert _last(env, tid) == ("operator", "text", "Here you go")


@pytest.mark.parametrize("kind", ["approval", "error", "event", "media"])
def test_the_other_kinds_with_text_write_it(env, kind):
    tid = env.thread()["id"]
    env.message(tid, role="operator", kind=kind, text="Something happened")
    assert _last(env, tid) == ("operator", kind, "Something happened")


def test_patching_a_message_keeps_the_last_line(env):
    tid = env.thread()["id"]
    m = env.message(tid, text="first")
    r = env.client.patch(f"/v1/chat/messages/{m['id']}", json={"text": "edited"}, headers=_h())
    assert r.status_code == 200
    assert _last(env, tid) == ("user", "text", "first")


def test_a_retried_message_post_does_not_rewrite_the_line(env):
    tid = env.thread()["id"]
    mid = str(uuid.uuid4())
    env.message(tid, id=mid, text="first")
    env.message(tid, text="second")
    env.message(tid, id=mid, text="first")  # the stored message comes back
    assert _last(env, tid)[2] == "second"


# ── the thumbnail ────────────────────────────────────────────────────────────


def test_a_production_picture_becomes_the_thumbnail_and_keeps_the_time(env):
    tid = env.thread()["id"]
    p = _new(env, tid)
    audio, image = _media(env, "audio"), _media(env, "image")
    before = _thread_row(env, tid).updated_at
    assert _patch(env, p["id"], add_media_ids=[audio, image]).status_code == 200
    after = _thread_row(env, tid)
    assert after.thumb_media_id == image
    assert after.updated_at == before


def test_the_newest_picture_of_a_patch_wins_and_audio_alone_does_nothing(env):
    tid = env.thread()["id"]
    p = _new(env, tid)
    first, second = _media(env, "image"), _media(env, "image")
    _patch(env, p["id"], add_media_ids=[first, second, _media(env, "audio")])
    assert _thread_row(env, tid).thumb_media_id == second
    _patch(env, p["id"], add_media_ids=[_media(env, "audio"), "not-a-uuid"])
    assert _thread_row(env, tid).thumb_media_id == second


def test_another_users_picture_is_not_a_thumbnail(env):
    tid = env.thread()["id"]
    p = _new(env, tid)
    assert _patch(env, p["id"], add_media_ids=[_media(env, "image", USER_B)]).status_code == 200
    assert _thread_row(env, tid).thumb_media_id is None


# ── the run count ────────────────────────────────────────────────────────────


def _run_count(env: Env, routine_id: str) -> int:
    async def q(s):
        return (await s.execute(select(Routine.run_count).where(Routine.id == routine_id))).scalar_one()

    return env.db(q)


def test_a_routine_counts_ok_and_failed_runs_but_not_skipped_ones(env):
    r = env.client.post(
        f"/v1/users/{USER_A}/routines",
        json={"name": "n", "prompt": "p", "cadence": "daily", "time_of_day": "09:00",
              "time_zone": "Europe/London", "next_run_at": AT.isoformat()},
        headers=_h(),
    ).json()
    assert _run_count(env, r["id"]) == 0
    for outcome, expected in (("ok", 1), ("failed", 2), ("skipped", 2), ("ok", 3)):
        out = env.client.post(
            f"/v1/routines/{r['id']}/outcome", json={"outcome": outcome, "at": AT.isoformat()}, headers=_h()
        )
        assert out.status_code == 200, out.text
        assert _run_count(env, r["id"]) == expected
