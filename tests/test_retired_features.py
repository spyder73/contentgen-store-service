"""Phase 5a: generator training is retired, its data stays.

The generator-profile, puppet-pose and dataset-template routes are gone, the
access defaults no longer list the generators, datasets and training features,
and chat is open to everyone on a new install. The tables, the model classes and
``characters.generator_profile_id`` stay, so old rows still resolve and a
character that still points at a profile still saves.
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
from app.models import AccessFeature, AccessFeatureUser, Character, ClipPrompt, MediaItem, Series, User  # noqa: E402
from app.stores import access  # noqa: E402

SECRET = "retired-features-secret"
USER_A = str(uuid.uuid4())
RETIRED_FEATURES = {"generators", "datasets", "training"}


def _headers(user: str | None = USER_A) -> dict[str, str]:
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
            AccessFeature.__table__,
            AccessFeatureUser.__table__,
        ):
            await conn.run_sync(lambda c, t=table: t.create(c))
        # characters.generator_profile_id references it; the real DDL carries
        # postgres-only defaults, so a stand-in with just the key is enough.
        await conn.execute(text("CREATE TABLE generator_profiles (id VARCHAR(36) PRIMARY KEY)"))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        s.add(User(id=USER_A, username="a", password_hash="x"))
        await s.commit()


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """(client, session factory) over a fresh sqlite database."""
    monkeypatch.setenv("INTERNAL_API_SECRET", SECRET)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'retired.db'}", poolclass=NullPool)
    asyncio.run(_create_tables(engine))
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _session():
        async with factory() as session:
            yield session

    app = create_fastapi_app()
    app.dependency_overrides[get_session] = _session
    with TestClient(app) as client:
        yield client, factory
    asyncio.run(engine.dispose())


def test_retired_routes_are_gone(db):
    client, _ = db
    for path in ("/v1/generator-profiles", "/v1/puppet-poses", "/v1/dataset-templates"):
        assert client.get(path, headers=_headers()).status_code == 404, path


def test_access_defaults_have_no_training_features_and_open_chat(db):
    _, factory = db
    defaults = dict(access.DEFAULT_FEATURES)
    assert not RETIRED_FEATURES & set(defaults)
    assert not RETIRED_FEATURES & set(access.TAB_FEATURES.values())
    assert not RETIRED_FEATURES & set(access.TAB_FEATURES)
    assert defaults["chat"] is False

    async def installed() -> dict[str, bool]:
        async with factory() as session:
            await access.ensure_defaults(session)
            await session.commit()
            return {row.feature_key: row.whitelist_enabled for row in await access.list_features(session)}

    rows = asyncio.run(installed())
    assert not RETIRED_FEATURES & set(rows)
    assert rows["chat"] is False


def test_a_user_with_no_grants_gets_chat_on_a_new_install(db):
    _, factory = db

    async def access_of_a_new_user():
        async with factory() as session:
            return await access.get_user_access(session, USER_A)

    got = asyncio.run(access_of_a_new_user())
    assert "chat" in got.allowed_features and "chat" in got.visible_tabs
    assert not RETIRED_FEATURES & set(got.allowed_features)
    assert not RETIRED_FEATURES & set(got.visible_tabs)


def test_a_character_with_a_retired_profile_still_saves(db):
    client, factory = db
    sid, cid, profile_id = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())

    async def add_profile_row():
        async with factory() as session:
            await session.execute(text("INSERT INTO generator_profiles (id) VALUES (:id)"), {"id": profile_id})
            await session.commit()

    asyncio.run(add_profile_row())
    made = client.put(f"/v1/series/{sid}", json={"id": sid, "name": "Show"}, headers=_headers())
    assert made.status_code == 200, made.text

    body = {"id": cid, "series_id": sid, "name": "Ada", "generator_profile_id": profile_id}
    saved = client.put(f"/v1/characters/{cid}", json=body, headers=_headers())
    assert saved.status_code == 200, saved.text
    assert saved.json()["generator_profile_id"] == profile_id

    renamed = client.put(f"/v1/characters/{cid}", json={**body, "name": "Ada L."}, headers=_headers())
    assert renamed.status_code == 200, renamed.text
    assert client.get(f"/v1/characters/{cid}", headers=_headers()).json()["generator_profile_id"] == profile_id
