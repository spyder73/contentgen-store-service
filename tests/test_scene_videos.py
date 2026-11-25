"""Scene videos: per-user rows, the 500 cap, the internal status list (migration 0052).

Same harness as ``test_routines.py``.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
from typing import get_args

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.schema import CreateTable
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db import get_session
from app.fastapi_app import create_fastapi_app
from app.models import ClipPrompt, MediaItem, RenderTemplate, SceneVideo
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
            for table in (RenderTemplate, ClipPrompt, MediaItem):  # the media table's own dialect-specific defaults
                ddl = str(CreateTable(table.__table__).compile(dialect=conn.dialect))
                await conn.exec_driver_sql(ddl.replace("'{}'::jsonb", "'{}'"))
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
    bundle = {"sha256": "ab", "image_ref": "img"}
    r = _patch(env, vid, status="built", files={"manifest.ts": "x"}, bundle=bundle, session_id="s1",
               woken_at="2026-11-21T10:00:00Z")
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["status"] == "built" and out["files"] == {"manifest.ts": "x"} and out["bundle"] == bundle
    assert out["woken_at"].startswith("2026-11-21T10:00:00")
    cleared = _patch(env, vid, session_id=None, woken_at=None).json()
    assert cleared["session_id"] is None and cleared["woken_at"] is None
    assert cleared["status"] == "built" and cleared["bundle"] == bundle  # untouched fields stay


def test_patch_validation(env):
    vid = _mk(env)["id"]
    assert _patch(env, vid, files={f"fixtures/f{i}.json": "x" for i in range(49)}).status_code == 422
    assert _patch(env, vid, files={f"fixtures/f{i}.json": "x" for i in range(48)}).status_code == 200
    assert _patch(env, vid, status="weird").status_code == 422
    assert _patch(env, vid, status=None).status_code == 422
    assert _patch(env, vid, user_id=USER_B).status_code == 422
    assert _patch(env, vid, files={"schema.ts": "x" * (64 * 1024 + 1)}).status_code == 422
    assert _patch(env, vid, files={"schema.ts": "x" * (64 * 1024 - len("schema.ts"))}).status_code == 200
    nine = {f"fixtures/f{i}.json": "x" * (64 * 1024 - 16) for i in range(9)}
    assert _patch(env, vid, files=nine).status_code == 422  # 576 KiB in all
    assert _patch(env, vid, files={f"fixtures/f{i}.json": "x" * (64 * 1024 - len(f"fixtures/f{i}.json")) for i in range(8)}).status_code == 200  # exactly 512 KiB


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


def _set_updated(env: Env, vid: str, when: datetime) -> None:
    async def go() -> None:
        async with env.factory() as s:
            row = await s.get(SceneVideo, vid)
            row.updated_at = when
            await s.commit()

    asyncio.run(go())


def test_internal_list_filters_unwoken_and_recent_newest_first(env):
    now = datetime.now(timezone.utc)
    old, mid, new, told = (_mk(env)["id"] for _ in range(4))
    for v in (old, mid, new, told):
        _patch(env, v, status="ready")
    _patch(env, told, status="ready", woken_at=now.isoformat())
    _set_updated(env, old, now - timedelta(days=3))
    _set_updated(env, mid, now - timedelta(hours=2))
    _set_updated(env, new, now - timedelta(hours=1))
    _set_updated(env, told, now)
    url = "/v1/internal/scene-videos?status=ready"
    ids = lambda q: [v["id"] for v in env.client.get(url + q, headers=_h()).json()]
    assert ids("&woken=false") == [new, mid, old]  # told excluded, newest first
    since = quote((now - timedelta(hours=24)).isoformat())
    assert ids(f"&woken=false&since={since}") == [new, mid]  # old excluded
    assert ids(f"&since={since}") == [told, new, mid]
    assert env.client.get(url + "&since=yesterday", headers=_h()).status_code == 422
    assert set(ids("")) == {old, mid, new, told}  # no params: as before


def test_the_action_literal_ends_with_the_scene_actions():
    assert get_args(ChatAction)[-4:] == ("scene.build", "scene.render", "routine.budget", "telegram.targets")


@pytest.mark.parametrize("action", ["scene.build", "scene.render"])
def test_a_scene_approval_row_is_accepted(env, action):
    subject = "scene:abc|v1"
    thread_id, approval_id, token = env.approved(action=action, subject=subject, amount_usd=0)
    stored = env.client.get(f"/v1/chat/approvals/{approval_id}", headers=_h()).json()
    assert (stored["action"], stored["status"]) == (action, "approved")
    r = env.consume(_consume_body(thread_id, token, action=action, subject=subject, needed_usd=0))
    assert r.status_code == 200 and r.json()["status"] == "used"


def _media(env: Env, user: str) -> str:
    import uuid

    mid = str(uuid.uuid4())

    async def add() -> None:
        async with env.factory() as s:
            s.add(MediaItem(id=mid, user_id=user, type="audio", prompt="p"))
            await s.commit()

    asyncio.run(add())
    return mid


def test_file_paths_must_be_package_paths_and_key_bytes_count(env):
    vid = _mk(env)["id"]
    for bad in ("../x.ts", "/abs.ts", "lib/../x.ts", "random.txt", "fixtures/constructor.json"):
        assert _patch(env, vid, files={bad: "x"}).status_code == 422, bad
    # the key's bytes count toward the per-file cap
    assert _patch(env, vid, files={"schema.ts": "x" * (64 * 1024 - 8)}).status_code == 422


def test_json_fields_are_capped_at_2_mib(env):
    vid = _mk(env)["id"]
    big = {"k": "x" * (2 * 1024 * 1024)}
    for name in ("plan", "last_report", "bundle", "voice_timeline"):
        assert _patch(env, vid, **{name: big}).status_code == 422, name
    assert _patch(env, vid, plan={"k": "x" * 1000}).status_code == 200


def test_another_users_thread_is_404_on_create_and_patch(env):
    theirs = env.thread(USER_B)["id"]
    r = env.client.post(f"/v1/users/{USER_A}/scene-videos", json={"thread_id": theirs}, headers=_h())
    assert r.status_code == 404 and r.json()["detail"] == "not_found"
    vid = _mk(env)["id"]
    assert _patch(env, vid, thread_id=theirs).status_code == 404
    mine = env.thread(USER_A)["id"]
    assert _mk(env, thread_id=mine)["thread_id"] == mine
    assert _patch(env, vid, thread_id=mine).status_code == 200


def test_media_references_must_be_the_users_own(env):
    vid = _mk(env)["id"]
    theirs, mine = _media(env, USER_B), _media(env, USER_A)
    for body in (
        {"narration_media_id": theirs},
        {"final_media_id": theirs},
        {"bundle": {"media_id": theirs, "sha256": "ab", "image_ref": "i"}},
        {"narration_media_id": "not-a-uuid"},
    ):
        r = _patch(env, vid, **body)
        assert r.status_code == 404 and r.json()["detail"] == "not_found", body
    ok = _patch(env, vid, narration_media_id=mine, final_media_id=mine, bundle={"media_id": mine, "sha256": "ab"})
    assert ok.status_code == 200 and ok.json()["narration_media_id"] == mine
    assert _patch(env, vid, narration_media_id=None).status_code == 200


def _takes(env: Env):
    m1, m2 = _media(env, USER_A), _media(env, USER_A)
    a = {"first": 0, "last": 1, "texts": ["one", "two"], "voice": "v1", "media_id": m1}
    b = {"first": 2, "last": 2, "texts": ["three"], "voice": "v1", "media_id": m2}
    return a, b


def test_voice_takes_round_trip_null_clears_and_default_null(env):
    vid = _mk(env)["id"]
    a, b = _takes(env)
    assert env.client.get(f"/v1/users/{USER_A}/scene-videos/{vid}", headers=_h()).json()["voice_takes"] is None
    out = _patch(env, vid, voice_takes=[a, b])
    assert out.status_code == 200, out.text
    assert out.json()["voice_takes"] == [a, b]
    got = env.client.get(f"/v1/users/{USER_A}/scene-videos/{vid}", headers=_h()).json()
    assert got["voice_takes"] == [a, b]
    assert _patch(env, vid, voice_takes=None, title="t").json()["voice_takes"] is None


def test_voice_take_media_must_be_the_users_own(env):
    vid = _mk(env)["id"]
    a, b = _takes(env)
    assert _patch(env, vid, voice_takes=[a]).status_code == 200
    theirs = _media(env, USER_B)
    r = _patch(env, vid, voice_takes=[a, {**b, "media_id": theirs}])
    assert r.status_code == 404 and r.json()["detail"] == "not_found"
    got = env.client.get(f"/v1/users/{USER_A}/scene-videos/{vid}", headers=_h()).json()
    assert got["voice_takes"] == [a]
    missing = "33333333-3333-4333-8333-333333333333"
    assert _patch(env, vid, voice_takes=[{**a, "media_id": missing}]).status_code == 404


def test_voice_takes_bounds(env):
    vid = _mk(env)["id"]
    a, _ = _takes(env)
    mid = a["media_id"]

    def bad(**over):
        return _patch(env, vid, voice_takes=[{**a, **over}]).status_code

    one = lambda i: {"first": i % 40, "last": i % 40, "texts": ["x"], "voice": "v", "media_id": mid}
    assert _patch(env, vid, voice_takes=[one(i) for i in range(40)]).status_code == 200
    assert _patch(env, vid, voice_takes=[one(i) for i in range(41)]).status_code == 422
    assert bad(first=2, last=1, texts=["a", "b"]) == 422
    assert bad(first=-1, last=0, texts=["a", "b"]) == 422
    assert bad(first=39, last=40, texts=["a", "b"]) == 422
    assert bad(texts=["only one"]) == 422
    assert bad(texts=["a", "x" * 501]) == 422
    assert bad(texts=["a", "x" * 500]) == 200
    assert bad(voice="v" * 201) == 422
    assert bad(voice="v" * 200) == 200
    assert bad(media_id="not-a-uuid") == 422
    assert bad(extra=1) == 422
    assert _patch(env, vid, voice_takes=[{"first": 0, "last": 0, "texts": ["a"], "voice": "v"}]).status_code == 422
    assert _patch(env, vid, voice_takes=[{**a, "first": 0, "last": 39, "texts": ["a"] * 40}]).status_code == 200


# ---- pictures (migration 0055) ----

SHA = "a" * 64


def _pic_media(env: Env, user: str, kind: str | None = "scene_picture") -> str:
    import uuid

    mid = str(uuid.uuid4())

    async def add() -> None:
        async with env.factory() as s:
            meta = {"kind": kind} if kind else {}
            s.add(MediaItem(id=mid, user_id=user, type="image", prompt="p", metadata_=meta))
            await s.commit()

    asyncio.run(add())
    return mid


def _pic(mid: str, label: str = "pic-one", **over) -> dict:
    return {"label": label, "media_id": mid, "sha256": SHA, "mime": "image/png", "bytes": 1000,
            "width": 640, "height": 480, "kind": "library", **over}


def _get(env: Env, vid: str) -> dict:
    return env.client.get(f"/v1/users/{USER_A}/scene-videos/{vid}", headers=_h()).json()


def test_pictures_round_trip_null_clears_and_omitting_keeps(env):
    vid = _mk(env)["id"]
    assert _get(env, vid)["pictures"] is None
    full = _pic(_pic_media(env, USER_A), "pic-one", credit="c", licence="CC BY 4.0",
                licence_url="https://creativecommons.org/licenses/by/4.0/",
                source_url="http://example.org/p", author="A. Person")
    other = _pic(_pic_media(env, USER_A), "pic-two", kind="generated")
    out = _patch(env, vid, pictures=[full, other])
    assert out.status_code == 200, out.text
    assert out.json()["pictures"][0] == full
    assert out.json()["pictures"][1] == {**other, "credit": None, "licence": None, "licence_url": None,
                                         "source_url": None, "author": None}
    got = _get(env, vid)["pictures"]
    assert got[0]["author"] == "A. Person" and got[1]["kind"] == "generated" and got[1]["width"] == 640
    assert got[0]["media_id"] == full["media_id"]
    assert _patch(env, vid, title="t").json()["pictures"] == got  # omitted: unchanged
    assert _patch(env, vid, pictures=[]).json()["pictures"] is None  # empty clears
    _patch(env, vid, pictures=[full])
    assert _patch(env, vid, pictures=None).status_code == 200
    assert _get(env, vid)["pictures"] is None


def test_pictures_cap_of_twelve_and_unique_labels(env):
    vid = _mk(env)["id"]
    mid = _pic_media(env, USER_A)
    twelve = [_pic(mid, f"pic-p{i}") for i in range(12)]
    assert _patch(env, vid, pictures=twelve).status_code == 200
    assert _patch(env, vid, pictures=[*twelve, _pic(mid, "pic-p12")]).status_code == 422
    assert _patch(env, vid, pictures=[_pic(mid, "pic-a"), _pic(mid, "pic-a")]).status_code == 422


def test_picture_media_must_be_the_users_own_and_a_scene_picture(env):
    vid = _mk(env)["id"]
    mine = _pic_media(env, USER_A)
    assert _patch(env, vid, pictures=[_pic(mine)]).status_code == 200
    theirs = _pic_media(env, USER_B)
    r = _patch(env, vid, pictures=[_pic(mine, "pic-a"), _pic(theirs, "pic-b")])
    assert r.status_code == 404 and r.json()["detail"] == "not_found"
    missing = "33333333-3333-4333-8333-333333333333"
    assert _patch(env, vid, pictures=[_pic(missing)]).status_code == 404
    for kind in (None, "clipstyle_asset", "generated"):
        wrong = _pic_media(env, USER_A, kind)
        r = _patch(env, vid, pictures=[_pic(wrong)])
        assert r.status_code == 422 and r.json()["detail"] == "not_a_scene_picture", kind
    assert [p["label"] for p in _get(env, vid)["pictures"]] == ["pic-one"]


def test_an_ownerless_scene_picture_is_not_the_users(env):
    # _get_owned lets a row with no owner through; a picture must be the user's own copy.
    vid = _mk(env)["id"]
    ownerless = _pic_media(env, None)
    r = _patch(env, vid, pictures=[_pic(_pic_media(env, USER_A), "pic-a"), _pic(ownerless, "pic-b")])
    assert r.status_code == 422 and r.json()["detail"] == "not_a_scene_picture"
    assert _get(env, vid)["pictures"] is None


def test_picture_field_bounds(env):
    vid = _mk(env)["id"]
    mid = _pic_media(env, USER_A)

    def code(**over):
        return _patch(env, vid, pictures=[_pic(mid, **over)]).status_code

    assert code() == 200
    for label in ("one", "pic-", "pic-A", "pic-a-b", "pic-" + "a" * 25, "pic-é"):
        assert code(label=label) == 422, label
    assert code(label="pic-" + "a" * 24) == 200
    assert code(media_id="not-a-uuid") == 422
    assert code(sha256="A" * 64) == 422 and code(sha256="a" * 63) == 422
    assert code(mime="image/gif") == 422 and code(mime="image/webp") == 200
    assert code(bytes=0) == 422 and code(bytes=5 * 1024 * 1024) == 200 and code(bytes=5 * 1024 * 1024 + 1) == 422
    assert code(width=0) == 422 and code(height=8193) == 422 and code(width=8192, height=4880) == 200
    assert code(width=8192, height=4883) == 422 and code(width=8192, height=8192) == 422
    assert code(credit="x" * 300) == 200 and code(credit="x" * 301) == 422
    assert code(licence="x" * 81) == 422 and code(author="x" * 201) == 422
    assert code(licence_url="ftp://x.org/a") == 422 and code(source_url="javascript:alert(1)") == 422
    assert code(source_url="https://x.org/" + "a" * 2000) == 422
    assert code(kind="screenshot") == 200 and code(kind="licensed") == 200 and code(kind="other") == 422
    assert code(extra=1) == 422
    bare = _pic(mid)
    del bare["kind"]
    assert _patch(env, vid, pictures=[bare]).status_code == 422
