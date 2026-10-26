"""PUT /v1/series/{id} and PUT /v1/characters/{id} write only the caller's own rows.

The backend's create routes take a client-chosen id, and the store's PUT is an
upsert: before this, a caller who knew another user's series or character id
could merge its fields into that row, and move a character into a series of
their own. Now a row that exists under another user answers 404 and nothing is
written, and a character's series must be the caller's, before and after.
These tests drive the real routes over sqlite, as tests/test_series_merge.py
does.
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
from app.models import Character, ClipPrompt, MediaItem, Series, User  # noqa: E402

SECRET = "series-ownership-secret"
USER_A = str(uuid.uuid4())
USER_B = str(uuid.uuid4())


def _headers(user: str | None) -> dict[str, str]:
    headers = {"X-Internal-Secret": SECRET}
    if user:
        headers["X-User-ID"] = user
    return headers


async def _create_tables(engine) -> None:
    async with engine.begin() as conn:
        for table in (
            User.__table__,
            ClipPrompt.__table__,
            MediaItem.__table__,
            Series.__table__,
            Character.__table__,
        ):
            await conn.run_sync(lambda c, t=table: t.create(c))
        await conn.execute(text("CREATE TABLE generator_profiles (id VARCHAR(36) PRIMARY KEY)"))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        s.add(User(id=USER_A, username="a", password_hash="x"))
        s.add(User(id=USER_B, username="b", password_hash="x"))
        await s.commit()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("INTERNAL_API_SECRET", SECRET)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'owner.db'}", poolclass=NullPool)
    asyncio.run(_create_tables(engine))
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _session():
        async with factory() as session:
            yield session

    app = create_fastapi_app()
    app.dependency_overrides[get_session] = _session
    with TestClient(app) as c:
        yield c
    asyncio.run(engine.dispose())


def _put_series(client, user, sid: str, **body):
    return client.put(f"/v1/series/{sid}", json={"id": sid, **body}, headers=_headers(user))


def _put_character(client, user, cid: str, sid: str, **body):
    return client.put(
        f"/v1/characters/{cid}", json={"id": cid, "series_id": sid, **body}, headers=_headers(user)
    )


def _series_of(client, user, name: str) -> str:
    sid = str(uuid.uuid4())
    r = _put_series(client, user, sid, name=name, description=f"{name}'s show", metadata={"cover": "palm"})
    assert r.status_code == 200, r.text
    return sid


def _character_of(client, user, sid: str, name: str) -> str:
    cid = str(uuid.uuid4())
    r = _put_character(client, user, cid, sid, name=name, description=f"{name} of the show")
    assert r.status_code == 200, r.text
    return cid


def _get(client, user, path: str) -> dict:
    r = client.get(path, headers=_headers(user))
    assert r.status_code == 200, r.text
    return r.json()


# ── series ───────────────────────────────────────────────────────────────────


def test_a_series_of_another_user_is_not_found_and_left_as_it_was(client):
    sid = _series_of(client, USER_A, "Coconut")

    for user in (USER_B, None):
        r = _put_series(client, user, sid, name="Taken", description="", metadata={})
        assert r.status_code == 404, r.text

    got = _get(client, USER_A, f"/v1/series/{sid}")
    assert (got["name"], got["description"], got["metadata"]) == ("Coconut", "Coconut's show", {"cover": "palm"})
    assert client.get(f"/v1/series/{sid}", headers=_headers(USER_B)).status_code == 404


def test_the_owner_still_writes_their_series(client):
    sid = _series_of(client, USER_A, "Coconut")

    assert _put_series(client, USER_A, sid, name="Coconut Again").status_code == 200

    assert _get(client, USER_A, f"/v1/series/{sid}")["name"] == "Coconut Again"


# ── characters ───────────────────────────────────────────────────────────────


def test_a_character_of_another_user_is_not_found_and_left_as_it_was(client):
    sid_a, sid_b = _series_of(client, USER_A, "Coconut"), _series_of(client, USER_B, "Mango")
    cid = _character_of(client, USER_A, sid_a, "Captain")

    # Neither in place nor moved into the caller's own series.
    for target in (sid_a, sid_b):
        r = _put_character(client, USER_B, cid, target, name="Taken", description="")
        assert r.status_code == 404, r.text
    assert _put_character(client, None, cid, sid_a, name="Taken").status_code == 404

    got = _get(client, USER_A, f"/v1/characters/{cid}")
    assert (got["series_id"], got["name"], got["description"]) == (sid_a, "Captain", "Captain of the show")


def test_a_character_is_never_moved_into_another_users_series(client):
    sid_a, sid_b = _series_of(client, USER_A, "Coconut"), _series_of(client, USER_B, "Mango")
    cid = _character_of(client, USER_A, sid_a, "Captain")

    r = _put_character(client, USER_A, cid, sid_b, name="Captain")
    assert r.status_code == 404, r.text

    got = _get(client, USER_A, f"/v1/characters/{cid}")
    assert got["series_id"] == sid_a


def test_a_new_character_in_another_users_series_is_refused(client):
    sid_a = _series_of(client, USER_A, "Coconut")
    cid = str(uuid.uuid4())

    for user in (USER_B, None):
        r = _put_character(client, user, cid, sid_a, name="Stowaway")
        assert r.status_code == 404, r.text

    assert client.get(f"/v1/characters/{cid}", headers=_headers(USER_A)).status_code == 404
    listed = _get(client, USER_A, f"/v1/characters?series_id={sid_a}")
    assert listed["total"] == 0


def test_the_owner_still_writes_and_moves_their_cast(client):
    sid_a, sid_a2 = _series_of(client, USER_A, "Coconut"), _series_of(client, USER_A, "Coconut 2")
    cid = _character_of(client, USER_A, sid_a, "Captain")

    assert _put_character(client, USER_A, cid, sid_a2, voice="warm").status_code == 200

    got = _get(client, USER_A, f"/v1/characters/{cid}")
    assert (got["series_id"], got["name"], got["voice"]) == (sid_a2, "Captain", "warm")
