"""video_analyses: a long video's analysis kept 30 days (migration 0049).

Same harness as ``test_playbooks.py``: a sqlite database built from the models with foreign keys
on. The migration's DDL is asserted by rendering it offline in the postgres dialect. Expiry is
lazy (no purge job): an expired row reads as absent and a save purges the caller's expired rows.
"""
from __future__ import annotations

import asyncio
import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, event, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db import get_session
from app.fastapi_app import create_fastapi_app
from app.models import User, VideoAnalysis
from app.schemas import VideoAnalysisIn
from app.stores import video_analyses as store
from tests.test_chat import SECRET, USER_A, USER_B, Env, _create_tables, _h, _render

MIGRATION_PATH = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0049_video_analyses.py"
VID = "dQw4w9WgXcQ"
VID2 = "abcdefghijk"

BODY = {
    "url": "https://www.youtube.com/watch?v=" + VID,
    "title": "A long talk",
    "channel": "Some channel",
    "duration_s": 5400.5,
    "transcript": {"segments": [{"start": 0, "text": "hello"}]},
    "signals": {"rows": [{"t": 0, "energy": 0.4}]},
    "candidates": [{"start": 10, "end": 40}],
}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("INTERNAL_API_SECRET", SECRET)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'va.db'}", poolclass=NullPool)

    @event.listens_for(engine.sync_engine, "connect")
    def _enable_foreign_keys(dbapi_conn, _record):  # pragma: no cover - trivial
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async def create() -> None:
        await _create_tables(engine)
        async with engine.begin() as conn:
            await conn.run_sync(lambda c: VideoAnalysis.__table__.create(c))

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


def _url(video: str = VID, user: str = USER_A, platform: str = "youtube") -> str:
    return f"/v1/users/{user}/video-analyses/by-video/{platform}/{video}"


def _put(env: Env, video: str = VID, user: str = USER_A, **over):
    body = {**BODY, **over}
    body = {k: v for k, v in body.items() if v is not ...}
    return env.client.put(_url(video, user), json=body, headers=_h(user))


def _saved(env: Env, video: str = VID, user: str = USER_A, **over) -> dict:
    r = _put(env, video, user, **over)
    assert r.status_code == 200, r.text
    return r.json()


def _expire(env: Env, video: str = VID, user: str = USER_A) -> None:
    async def go(s):
        await s.execute(
            update(VideoAnalysis)
            .where(VideoAnalysis.user_id == user, VideoAnalysis.video_id == video)
            .values(expires_at=datetime.now(timezone.utc) - timedelta(minutes=1))
        )
        await s.commit()

    env.db(go)


def _picks(env: Env, analysis_id: str, brief: str = "funny bits", user: str = USER_A):
    return env.client.post(
        f"/v1/users/{user}/video-analyses/{analysis_id}/picks",
        json={"brief": brief, "moments": [{"start": 1, "end": 9}]},
        headers=_h(user),
    )


def test_put_then_get_returns_the_row_with_a_30_day_expiry(env):
    out = _saved(env)
    assert (out["user_id"], out["platform"], out["video_id"]) == (USER_A, "youtube", VID)
    assert (out["title"], out["channel"], out["duration_s"]) == ("A long talk", "Some channel", 5400.5)
    assert out["picks"] == [] and out["candidates"] == BODY["candidates"]
    assert out["transcript"] == BODY["transcript"] and out["signals"] == BODY["signals"]
    created = datetime.fromisoformat(out["created_at"].rstrip("Z"))
    expires = datetime.fromisoformat(out["expires_at"].rstrip("Z"))
    assert expires - created == timedelta(days=30)
    got = env.client.get(_url(), headers=_h())
    assert got.status_code == 200
    # sqlite hands back naive datetimes, so the two responses differ only by a trailing Z.
    stamps = ("created_at", "expires_at")
    assert {k: v for k, v in got.json().items() if k not in stamps} == {k: v for k, v in out.items() if k not in stamps}
    assert got.json()["expires_at"].rstrip("Z") == out["expires_at"].rstrip("Z")
    assert set(out) == {
        "id", "user_id", "platform", "video_id", "url", "title", "channel", "duration_s",
        "transcript", "signals", "candidates", "picks", "created_at", "expires_at",
    }


def test_a_missing_transcript_is_accepted_as_null_or_absent(env):
    assert _put(env, transcript=None).status_code == 200
    assert env.client.get(_url(), headers=_h()).json()["transcript"] is None
    assert _put(env, VID2, transcript=...).status_code == 200
    assert env.client.get(_url(VID2), headers=_h()).json()["transcript"] is None


def test_put_again_replaces_and_clears_picks(env):
    first = _saved(env)
    assert _picks(env, first["id"]).status_code == 200
    second = _saved(env, title="Renamed")
    assert second["title"] == "Renamed" and second["picks"] == []
    assert second["id"] == first["id"]
    assert env.count(VideoAnalysis) == 1


def test_an_expired_row_reads_as_missing_and_a_put_replaces_it(env):
    first = _saved(env)
    _expire(env)
    r = env.client.get(_url(), headers=_h())
    assert (r.status_code, r.json()["detail"]) == (404, "video_analysis_not_found")
    assert _picks(env, first["id"]).status_code == 404
    again = _saved(env, title="Fresh")
    assert again["title"] == "Fresh"
    assert env.count(VideoAnalysis) == 1
    assert env.client.get(_url(), headers=_h()).status_code == 200


def test_a_save_purges_the_callers_other_expired_rows(env):
    _saved(env, VID2)
    _saved(env, VID2, user=USER_B)
    _expire(env, VID2)
    _expire(env, VID2, user=USER_B)
    _saved(env, VID)
    assert env.count(VideoAnalysis, VideoAnalysis.user_id == USER_A) == 1
    assert env.count(VideoAnalysis, VideoAnalysis.user_id == USER_B) == 1  # not the caller's


def test_picks_append_and_keep_the_newest_20(env):
    a = _saved(env)
    for i in range(22):
        r = _picks(env, a["id"], brief=f"brief {i}")
        assert r.status_code == 200, r.text
    picks = r.json()["picks"]
    assert len(picks) == 20
    assert [p["brief"] for p in picks][0] == "brief 2" and picks[-1]["brief"] == "brief 21"
    assert set(picks[-1]) == {"brief", "created_at", "moments"}
    assert picks[-1]["moments"] == [{"start": 1, "end": 9}]
    assert env.client.get(_url(), headers=_h()).json()["picks"] == picks


def test_another_users_row_is_404_and_the_path_user_must_be_the_header_user(env):
    a = _saved(env)
    assert env.client.get(_url(user=USER_B), headers=_h(USER_B)).status_code == 404
    assert _picks(env, a["id"], user=USER_B).status_code == 404
    theirs = _h(USER_B)
    assert env.client.get(_url(), headers=theirs).status_code == 403
    assert env.client.put(_url(), json=BODY, headers=theirs).status_code == 403
    r = env.client.post(
        f"/v1/users/{USER_A}/video-analyses/{a['id']}/picks", json={"brief": "x", "moments": []}, headers=theirs
    )
    assert r.status_code == 403
    assert env.client.get(_url(), headers=_h()).json()["picks"] == []


def test_deleting_the_user_deletes_their_analyses(env):
    _saved(env, user=USER_A)
    _saved(env, user=USER_B)

    async def drop(session):
        await session.execute(delete(User).where(User.id == USER_A))
        await session.commit()

    env.db(drop)
    assert env.count(VideoAnalysis, VideoAnalysis.user_id == USER_A) == 0
    assert env.count(VideoAnalysis, VideoAnalysis.user_id == USER_B) == 1


def test_a_bad_platform_or_video_id_or_oversized_transcript_is_422(env):
    assert env.client.get(_url(platform="vimeo"), headers=_h()).status_code == 422
    assert env.client.put(_url(platform="vimeo"), json=BODY, headers=_h()).status_code == 422
    for bad in ("short", "waytoolongvideoid", "bad id here"[:11], "abc!efghijk"):
        assert env.client.put(_url(bad), json=BODY, headers=_h()).status_code == 422, bad
    big = {"segments": [0] * 12001}
    assert _put(env, transcript=big).status_code == 422
    assert _put(env, transcript={"segments": [0] * 12000}).status_code == 200
    assert _put(env, signals={"rows": [0] * 1201}).status_code == 422
    assert _put(env, candidates=[0] * 41).status_code == 422
    assert _put(env, duration_s=0).status_code == 422
    assert _put(env, duration_s=10801).status_code == 422
    assert _put(env, title="t" * 301).status_code == 422
    a = _saved(env, VID2)
    for brief in ("", "b" * 501):
        r = env.client.post(
            f"/v1/users/{USER_A}/video-analyses/{a['id']}/picks", json={"brief": brief, "moments": []}, headers=_h()
        )
        assert r.status_code == 422
    r = env.client.post(
        f"/v1/users/{USER_A}/video-analyses/{a['id']}/picks", json={"brief": "x", "moments": [{}] * 21}, headers=_h()
    )
    assert r.status_code == 422


def _load_migration_module():
    spec = importlib.util.spec_from_file_location("migration_0049_video_analyses", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_0049_creates_the_table_with_its_unique_key_and_cascade():
    module = _load_migration_module()
    assert module.revision == "0049" and module.down_revision == "0048"
    sql = _render(module.upgrade)
    assert "CREATE TABLE video_analyses" in sql
    assert "CONSTRAINT uq_video_analyses_user_platform_video UNIQUE (user_id, platform, video_id)" in sql
    assert "FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE" in sql
    assert "CHECK (platform IN ('youtube'))" in sql
    assert "DEFAULT '[]'::jsonb" in sql
    assert "DROP TABLE video_analyses" in _render(module.downgrade)


# ── fix round 1: bounds and concurrency ──────────────────────────────────────


def test_a_field_over_its_byte_cap_is_413_with_a_fixed_detail(env):
    # Few items, so the count caps pass; each blob is one big string.
    cases = (
        ("transcript", {"segments": [{"text": "x" * (4 * 1024 * 1024)}]}, "transcript_too_large: over 4 MiB"),
        ("signals", {"rows": [{"x": "x" * (1024 * 1024)}]}, "signals_too_large: over 1 MiB"),
        ("candidates", [{"x": "x" * (256 * 1024)}], "candidates_too_large: over 256 KiB"),
    )
    for field, value, detail in cases:
        r = _put(env, **{field: value})
        assert (r.status_code, r.json()["detail"]) == (413, detail), field
    assert env.count(VideoAnalysis) == 0
    a = _saved(env)
    r = env.client.post(
        f"/v1/users/{USER_A}/video-analyses/{a['id']}/picks",
        json={"brief": "x", "moments": [{"x": "x" * (64 * 1024)}]},
        headers=_h(),
    )
    assert (r.status_code, r.json()["detail"]) == (413, "pick_too_large: over 64 KiB")
    assert env.client.get(_url(), headers=_h()).json()["picks"] == []


def test_the_201st_live_analysis_is_refused_but_an_update_is_not(env, monkeypatch):
    monkeypatch.setattr(store, "MAX_LIVE_PER_USER", 3)
    for vid in ("aaaaaaaaaa1", "aaaaaaaaaa2", "aaaaaaaaaa3"):
        _saved(env, vid)
    r = _put(env, "aaaaaaaaaa4")
    assert (r.status_code, r.json()["detail"]) == (409, "video_analysis_limit_reached")
    assert env.count(VideoAnalysis) == 3
    assert _put(env, "aaaaaaaaaa2", title="again").status_code == 200  # an update at the cap
    assert _saved(env, "aaaaaaaaaa4", user=USER_B)["video_id"] == "aaaaaaaaaa4"  # another user's count
    _expire(env, "aaaaaaaaaa1")  # expired rows do not count
    assert _put(env, "aaaaaaaaaa4").status_code == 200


def test_a_lost_race_on_the_first_save_updates_the_winners_row(env, monkeypatch):
    winner = _saved(env, title="winner")
    real = store._find
    calls = []

    async def blind_first(*args, **kwargs):
        calls.append(1)
        return None if len(calls) == 1 else await real(*args, **kwargs)

    monkeypatch.setattr(store, "_find", blind_first)
    r = _put(env, title="loser")  # looks up nothing, inserts, loses the unique key, retries
    assert r.status_code == 200, r.text
    assert len(calls) == 2
    assert (r.json()["id"], r.json()["title"]) == (winner["id"], "loser")
    assert env.count(VideoAnalysis) == 1


def test_sequential_picks_posts_keep_both_sets(env):
    # The row lock (SELECT ... FOR UPDATE) only matters on Postgres; sqlite ignores it.
    a = _saved(env)
    assert _picks(env, a["id"], brief="first").status_code == 200
    assert _picks(env, a["id"], brief="second").status_code == 200
    assert [p["brief"] for p in env.client.get(_url(), headers=_h()).json()["picks"]] == ["first", "second"]


def test_the_users_row_is_locked_before_the_live_count(env, monkeypatch):
    # The lock is a no-op on sqlite; this pins the order that makes the cap safe on Postgres.
    events = []
    real_lock = store._lock_user_row

    async def spy_lock(session, user_id):
        events.append(("lock", user_id))
        await real_lock(session, user_id)

    monkeypatch.setattr(store, "_lock_user_row", spy_lock)

    real_count = store.func.count

    class FuncSpy:
        def __getattr__(self, name):
            return getattr(store.func, name)

        def count(self, *a, **k):
            events.append(("count", None))
            return real_count(*a, **k)

    monkeypatch.setattr(store, "func", FuncSpy())
    assert _put(env).status_code == 200
    assert events == [("lock", USER_A), ("count", None)]
