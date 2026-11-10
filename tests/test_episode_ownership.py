"""PUT /v1/episodes/{id} writes only into the caller's series; a PUT with no caller is refused.

Before this, the store's PUT merged into an episode by id and could move it into any
series. Ownership runs through the parent series, as on the by-id routes: a series with
no owner (legacy) stays reachable, a series of another user answers as a series that is
not there, and no caller is a 401 as on GET /v1/series. The lists are in
tests/test_series_lists_ownership.py. These tests drive the real routes over sqlite.
"""
from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

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
from app.models import Character, ClipPrompt, Episode, MediaItem, Series, User  # noqa: E402

SECRET = "episode-ownership-secret"
USER_A = str(uuid.uuid4())
USER_B = str(uuid.uuid4())


def _headers(user: str | None) -> dict[str, str]:
    return {"X-Internal-Secret": SECRET, **({"X-User-ID": user} if user else {})}


async def _create_tables(engine) -> None:
    async with engine.begin() as conn:
        for table in (User, ClipPrompt, MediaItem, Series, Character, Episode):
            await conn.run_sync(lambda c, t=table.__table__: t.create(c))
        await conn.execute(text("CREATE TABLE generator_profiles (id VARCHAR(36) PRIMARY KEY)"))
    async with async_sessionmaker(engine, expire_on_commit=False)() as s:
        s.add_all([User(id=u, username=n, password_hash="x") for u, n in ((USER_A, "a"), (USER_B, "b"))])
        await s.commit()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("INTERNAL_API_SECRET", SECRET)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'episodes.db'}", poolclass=NullPool)
    asyncio.run(_create_tables(engine))
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _session():
        async with factory() as session:
            yield session

    app = create_fastapi_app()
    app.dependency_overrides[get_session] = _session
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, factory=factory)
    asyncio.run(engine.dispose())


def _row(env, model, row_id):
    async def go():
        async with env.factory() as s:
            return await s.get(model, row_id)

    return asyncio.run(go())


def _legacy_series(env) -> str:
    """A series from before ownership: no user."""
    sid = str(uuid.uuid4())

    async def go():
        async with env.factory() as s:
            s.add(Series(id=sid, name="Legacy", user_id=None))
            await s.commit()

    asyncio.run(go())
    return sid


def _series(env, user, name="Coconut") -> str:
    sid = str(uuid.uuid4())
    r = env.client.put(f"/v1/series/{sid}", json={"id": sid, "name": name}, headers=_headers(user))
    assert r.status_code == 200, r.text
    return sid


def _put_episode(env, user, eid, sid, number=1, **body):
    payload = {"id": eid, "series_id": sid, "episode_number": number, "title": "Pilot", **body}
    return env.client.put(f"/v1/episodes/{eid}", json=payload, headers=_headers(user))


def _episode(env, user, sid, number=1) -> str:
    eid = str(uuid.uuid4())
    assert _put_episode(env, user, eid, sid, number).status_code == 200
    return eid


def _character(env, user, sid, name="Captain") -> str:
    cid = str(uuid.uuid4())
    r = env.client.put(
        f"/v1/characters/{cid}", json={"id": cid, "series_id": sid, "name": name}, headers=_headers(user)
    )
    assert r.status_code == 200, r.text
    return cid


def _listed(env, user, path) -> set[str]:
    r = env.client.get(path, headers=_headers(user))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == len(body["items"])
    return {item["id"] for item in body["items"]}


# ── PUT /v1/episodes/{id} ────────────────────────────────────────────────────


def test_the_owner_writes_and_moves_their_episodes(env):
    sid, sid2 = _series(env, USER_A), _series(env, USER_A, "Coconut 2")
    eid = _episode(env, USER_A, sid)

    assert _put_episode(env, USER_A, eid, sid, title="Renamed", status="running").status_code == 200
    assert _put_episode(env, USER_A, eid, sid2, number=4, title="Renamed").status_code == 200

    row = _row(env, Episode, eid)
    assert (row.series_id, row.title, row.status, row.episode_number) == (sid2, "Renamed", "running", 4)


def test_an_episode_of_another_user_is_not_found_and_left_as_it_was(env):
    sid_a, sid_b = _series(env, USER_A), _series(env, USER_B, "Mango")
    eid = _put_episode(env, USER_A, str(uuid.uuid4()), sid_a, status="running", title="Pilot").json()["id"]

    # Neither in place nor moved into the caller's own series.
    for target in (sid_a, sid_b):
        r = _put_episode(env, USER_B, eid, target, title="Taken", status="draft")
        assert (r.status_code, r.json()["detail"]) == (404, "not_found")

    row = _row(env, Episode, eid)
    assert (row.series_id, row.title, row.status) == (sid_a, "Pilot", "running")


def test_a_foreign_episode_answers_exactly_as_a_missing_one(env):
    sid_a, sid_b = _series(env, USER_A), _series(env, USER_B, "Mango")
    foreign = _episode(env, USER_A, sid_a)

    # Same caller, same target series: only whether the episode id exists differs.
    for target in (sid_a, str(uuid.uuid4())):
        r_foreign = _put_episode(env, USER_B, foreign, target)
        r_missing = _put_episode(env, USER_B, str(uuid.uuid4()), target)
        assert (r_foreign.status_code, r_foreign.json()) == (r_missing.status_code, r_missing.json())
        assert r_foreign.status_code == 404
    assert _put_episode(env, USER_B, str(uuid.uuid4()), sid_b).status_code == 200


def test_an_episode_is_never_put_into_another_users_series(env):
    sid_a, sid_b = _series(env, USER_A), _series(env, USER_B, "Mango")
    mine, new = _episode(env, USER_A, sid_a), str(uuid.uuid4())

    assert _put_episode(env, USER_A, mine, sid_b).status_code == 404  # moved
    assert _put_episode(env, USER_A, new, sid_b).status_code == 404  # created
    assert _put_episode(env, USER_A, str(uuid.uuid4()), str(uuid.uuid4())).status_code == 404  # no series

    assert _row(env, Episode, mine).series_id == sid_a
    assert _row(env, Episode, new) is None
    assert _listed(env, USER_B, f"/v1/episodes?series_id={sid_b}") == set()


def test_a_legacy_series_with_no_owner_stays_reachable(env):
    legacy = _legacy_series(env)
    eid = _episode(env, USER_A, legacy)

    assert _put_episode(env, USER_B, eid, legacy, title="Both write").status_code == 200

    assert _row(env, Episode, eid).title == "Both write"
    assert _row(env, Series, legacy).user_id is None  # a write never claims it


def test_a_put_with_no_caller_is_refused_and_writes_nothing(env):
    sid_a, legacy = _series(env, USER_A), _legacy_series(env)
    eid = _episode(env, USER_A, sid_a)

    for target in (sid_a, legacy):
        for episode_id in (eid, str(uuid.uuid4())):
            r = _put_episode(env, None, episode_id, target, title="Anon")
            assert (r.status_code, r.json()["detail"]) == (401, "user_id_required")

    assert _row(env, Episode, eid).title == "Pilot"
    assert _listed(env, USER_A, "/v1/episodes") == {eid}


# ── PUT /v1/series/{id} and /v1/characters/{id}: no caller ───────────────────


def test_a_series_put_with_no_caller_neither_creates_nor_changes_a_row(env):
    legacy, new = _legacy_series(env), str(uuid.uuid4())

    for sid in (legacy, new):
        r = env.client.put(f"/v1/series/{sid}", json={"id": sid, "name": "Anon"}, headers=_headers(None))
        assert (r.status_code, r.json()["detail"]) == (401, "user_id_required")

    assert _row(env, Series, legacy).name == "Legacy"
    assert _row(env, Series, new) is None  # no ownerless series is ever minted


def test_a_user_write_never_takes_over_a_legacy_row(env):
    legacy = _legacy_series(env)
    cid = _character(env, USER_A, legacy)

    r = env.client.put(f"/v1/series/{legacy}", json={"id": legacy, "name": "Renamed"}, headers=_headers(USER_A))
    assert r.status_code == 200 and _row(env, Series, legacy).user_id is None

    r = env.client.put(
        f"/v1/characters/{cid}", json={"id": cid, "series_id": legacy, "name": "Cap"}, headers=_headers(None)
    )
    assert r.status_code == 401
    assert _row(env, Character, cid).name == "Captain"
