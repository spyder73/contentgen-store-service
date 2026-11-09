"""The by-id clip routes answer only for the clip's owner.

Before this, GET/PATCH/DELETE/PUT /v1/clips/{id}, GET /v1/clips/{id}/full and
POST /v1/clips/{id}/swap loaded a clip by id alone: any caller who knew another
user's clip id could read it, change it, delete it or overwrite it with a PUT.
Now the caller comes from X-User-ID, and a clip owned by someone else answers
exactly as a missing one (404 not_found, clip_not_found for swap) and is left
untouched. No header is an internal job (pipeline runner, scheduler) and still
works; a legacy clip with no owner stays reachable. These tests drive the real
routes over sqlite, as tests/test_series_ownership.py does.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import NullPool


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(element, compiler, **kw):  # pragma: no cover - trivial
    return "JSON"


@compiles(UUID, "sqlite")
def _uuid_sqlite(element, compiler, **kw):  # pragma: no cover - trivial
    return "VARCHAR(36)"


from app.db import get_session  # noqa: E402
from app.fastapi_app import create_fastapi_app  # noqa: E402
from app.models import ClipPrompt, MediaItem, User  # noqa: E402

SECRET = "clip-ownership-secret"
USER_A = str(uuid.uuid4())
USER_B = str(uuid.uuid4())
LEGACY = "legacy"  # a clip whose user_id is NULL


def _headers(user: str | None) -> dict[str, str]:
    headers = {"X-Internal-Secret": SECRET}
    if user:
        headers["X-User-ID"] = user
    return headers


async def _create_tables(engine) -> None:
    async with engine.begin() as conn:
        for table in (User.__table__, ClipPrompt.__table__, MediaItem.__table__):
            await conn.run_sync(lambda c, t=table: t.create(c))
        await conn.execute(text("CREATE TABLE generator_profiles (id VARCHAR(36) PRIMARY KEY)"))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        s.add(User(id=USER_A, username="a", password_hash="x"))
        s.add(User(id=USER_B, username="b", password_hash="x"))
        await s.commit()


class Env:
    def __init__(self, client: TestClient, factory) -> None:
        self.client, self.factory = client, factory

    def seed(self, *rows) -> None:
        async def _go():
            async with self.factory() as s:
                s.add_all(rows)
                await s.commit()

        asyncio.run(_go())

    def row(self, clip_id: str) -> ClipPrompt | None:
        async def _go():
            async with self.factory() as s:
                return await s.get(ClipPrompt, clip_id)

        return asyncio.run(_go())

    def clip(self, user: str | None, name: str = "Original") -> str:
        """A clip owned by ``user`` (None: a legacy clip with no owner), with one image."""
        cid, mid = str(uuid.uuid4()), str(uuid.uuid4())
        self.seed(
            MediaItem(id=mid, user_id=user, type="image", prompt="old"),
            ClipPrompt(
                id=cid,
                user_id=user,
                name=name,
                media_refs={"images": [mid], "ai_videos": [], "audios": []},
            ),
        )
        return cid

    def new_media(self, user: str | None) -> str:
        mid = str(uuid.uuid4())
        self.seed(MediaItem(id=mid, user_id=user, type="image", prompt="new"))
        return mid

    def images(self, clip_id: str) -> list[str]:
        return self.row(clip_id).media_refs["images"]


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("INTERNAL_API_SECRET", SECRET)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'clips.db'}", poolclass=NullPool)
    asyncio.run(_create_tables(engine))
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _session():
        async with factory() as session:
            yield session

    app = create_fastapi_app()
    app.dependency_overrides[get_session] = _session
    with TestClient(app) as c:
        yield Env(c, factory)
    asyncio.run(engine.dispose())


def _put(env: Env, user: str | None, cid: str, **body):
    return env.client.put(f"/v1/clips/{cid}", json={"id": cid, **body}, headers=_headers(user))


def _swap(env: Env, user: str | None, cid: str, old: str, new: str):
    body = {"kind": "image", "media_index": 0, "old_media_id": old, "new_media_id": new}
    return env.client.post(f"/v1/clips/{cid}/swap", json=body, headers=_headers(user))


# ── another user's clip is a missing clip, on every route ───────────────────


def test_another_users_clip_cannot_be_read(env):
    cid = env.clip(USER_A)
    missing = str(uuid.uuid4())

    for path in (f"/v1/clips/{cid}", f"/v1/clips/{cid}/full"):
        r = env.client.get(path, headers=_headers(USER_B))
        assert (r.status_code, r.json()) == (404, {"detail": "not_found"}), path
    # Indistinguishable from an id that does not exist.
    r = env.client.get(f"/v1/clips/{missing}", headers=_headers(USER_B))
    assert (r.status_code, r.json()) == (404, {"detail": "not_found"})


def test_another_users_clip_cannot_be_patched(env):
    cid = env.clip(USER_A)

    r = env.client.patch(f"/v1/clips/{cid}", json={"name": "Taken"}, headers=_headers(USER_B))

    assert (r.status_code, r.json()) == (404, {"detail": "not_found"})
    row = env.row(cid)
    assert (row.name, row.user_id, row.is_dirty) == ("Original", USER_A, False)


def test_another_users_clip_cannot_be_deleted(env):
    cid = env.clip(USER_A)

    r = env.client.delete(f"/v1/clips/{cid}", headers=_headers(USER_B))

    assert (r.status_code, r.json()) == (404, {"detail": "not_found"})
    assert env.row(cid) is not None


def test_another_users_clip_cannot_be_swapped(env):
    cid = env.clip(USER_A)
    old = env.images(cid)[0]

    r = _swap(env, USER_B, cid, old, env.new_media(USER_B))

    assert (r.status_code, r.json()) == (404, {"detail": "clip_not_found"})
    assert env.images(cid) == [old]
    assert env.row(cid).is_dirty is False


def test_another_users_clip_cannot_be_overwritten_by_a_put(env):
    cid = env.clip(USER_A)

    r = _put(env, USER_B, cid, name="Taken", style={"look": "mine"})

    assert (r.status_code, r.json()) == (404, {"detail": "not_found"})
    row = env.row(cid)
    assert (row.name, row.user_id, row.style) == ("Original", USER_A, {})


# ── the owner is unaffected ──────────────────────────────────────────────────


def test_the_owner_reads_their_clip_and_sees_the_owner(env):
    cid = env.clip(USER_A)

    got = env.client.get(f"/v1/clips/{cid}", headers=_headers(USER_A))
    full = env.client.get(f"/v1/clips/{cid}/full", headers=_headers(USER_A))

    assert got.status_code == 200 and got.json()["user_id"] == USER_A
    assert full.status_code == 200
    assert full.json()["clip"]["user_id"] == USER_A
    assert [m["id"] for m in full.json()["media"]] == env.images(cid)


def test_the_owner_patches_swaps_puts_and_deletes_their_clip(env):
    cid = env.clip(USER_A)
    old, new = env.images(cid)[0], env.new_media(USER_A)

    r = env.client.patch(f"/v1/clips/{cid}", json={"name": "Renamed"}, headers=_headers(USER_A))
    assert r.status_code == 200 and r.json()["name"] == "Renamed"

    r = _swap(env, USER_A, cid, old, new)
    assert r.status_code == 200 and env.images(cid) == [new]

    r = _put(env, USER_A, cid, name="Put")
    assert r.status_code == 200 and r.json()["user_id"] == USER_A
    assert env.row(cid).name == "Put"

    assert env.client.delete(f"/v1/clips/{cid}", headers=_headers(USER_A)).status_code == 204
    assert env.row(cid) is None


def test_a_new_clip_put_belongs_to_the_caller(env):
    cid = str(uuid.uuid4())

    r = _put(env, USER_B, cid, name="Mine")

    assert r.status_code == 200 and r.json()["user_id"] == USER_B
    assert env.row(cid).user_id == USER_B
    assert env.client.get(f"/v1/clips/{cid}", headers=_headers(USER_A)).status_code == 404


# ── no header: an internal job, as before ────────────────────────────────────


def test_without_a_user_every_route_still_works(env):
    cid = env.clip(USER_A)
    old, new = env.images(cid)[0], env.new_media(USER_A)

    assert env.client.get(f"/v1/clips/{cid}", headers=_headers(None)).status_code == 200
    assert env.client.get(f"/v1/clips/{cid}/full", headers=_headers(None)).status_code == 200
    r = env.client.patch(f"/v1/clips/{cid}", json={"name": "Job"}, headers=_headers(None))
    assert r.status_code == 200 and r.json()["name"] == "Job"
    assert _swap(env, None, cid, old, new).status_code == 200
    assert _put(env, None, cid, name="Job put").status_code == 200
    # An update never changes the owner.
    assert env.row(cid).user_id == USER_A
    assert env.client.delete(f"/v1/clips/{cid}", headers=_headers(None)).status_code == 204
    assert env.row(cid) is None


def test_a_new_clip_put_without_a_user_has_no_owner(env):
    cid = str(uuid.uuid4())

    r = _put(env, None, cid, name="Job")

    assert r.status_code == 200 and r.json()["user_id"] is None
    assert env.row(cid).user_id is None


# ── a legacy clip with no owner stays reachable ──────────────────────────────


def test_a_clip_with_no_owner_is_reachable_by_any_user(env):
    cid = env.clip(None)
    old, new = env.images(cid)[0], env.new_media(None)

    for user in (USER_A, USER_B):
        got = env.client.get(f"/v1/clips/{cid}", headers=_headers(user))
        assert got.status_code == 200 and got.json()["user_id"] is None
        assert env.client.get(f"/v1/clips/{cid}/full", headers=_headers(user)).status_code == 200
    r = env.client.patch(f"/v1/clips/{cid}", json={"name": "Adopted"}, headers=_headers(USER_A))
    assert r.status_code == 200 and r.json()["name"] == "Adopted"
    assert _swap(env, USER_B, cid, old, new).status_code == 200
    assert _put(env, USER_A, cid, name="Put").status_code == 200
    assert env.client.delete(f"/v1/clips/{cid}", headers=_headers(USER_B)).status_code == 204
    assert env.row(cid) is None
