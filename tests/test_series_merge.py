"""PUT /v1/series/{id} and PUT /v1/characters/{id} merge with the stored row.

The operator's series tools send only the keys they change ("a field you leave
out is left as it is"). Before this, the store required ``name`` and blanked
``description``, ``concept`` and ``metadata`` (series) and ``description``,
``voice``, ``reference_image_media_id``, ``generator_profile_id`` and
``metadata`` (cast) whenever the body left them out. These tests drive the real
routes over in-memory-style sqlite, so absent / null / present are told apart
exactly as they are in production (``model_fields_set``).
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

SECRET = "series-merge-secret"
USER_A = str(uuid.uuid4())
HEADERS = {"X-Internal-Secret": SECRET, "X-User-ID": USER_A}


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
        # characters.generator_profile_id points at it; a stand-in with just the
        # referenced key is enough (the real DDL carries postgres-only defaults).
        await conn.execute(text("CREATE TABLE generator_profiles (id VARCHAR(36) PRIMARY KEY)"))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        s.add(User(id=USER_A, username="a", password_hash="x"))
        await s.commit()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("INTERNAL_API_SECRET", SECRET)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'merge.db'}", poolclass=NullPool)
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


def _put_series(client, sid: str, **body):
    return client.put(f"/v1/series/{sid}", json={"id": sid, **body}, headers=HEADERS)


def _get_series(client, sid: str) -> dict:
    r = client.get(f"/v1/series/{sid}", headers=HEADERS)
    assert r.status_code == 200, r.text
    return r.json()


def _put_character(client, cid: str, sid: str, **body):
    return client.put(
        f"/v1/characters/{cid}", json={"id": cid, "series_id": sid, **body}, headers=HEADERS
    )


def _get_character(client, cid: str) -> dict:
    r = client.get(f"/v1/characters/{cid}", headers=HEADERS)
    assert r.status_code == 200, r.text
    return r.json()


def _seed_series(client) -> str:
    sid = str(uuid.uuid4())
    r = _put_series(
        client,
        sid,
        name="Coconut Chronicles",
        description="A show about a coconut.",
        concept="The coconut is never found on screen.",
        template_id="reel-v3",
        metadata={"cover": "palm"},
    )
    assert r.status_code == 200, r.text
    return sid


# ── series ───────────────────────────────────────────────────────────────────


def test_sparse_series_update_keeps_description_concept_metadata(client):
    sid = _seed_series(client)

    r = _put_series(client, sid, template_id="reel-v4")

    assert r.status_code == 200, r.text
    got = _get_series(client, sid)
    assert got["template_id"] == "reel-v4"
    assert got["name"] == "Coconut Chronicles"
    assert got["description"] == "A show about a coconut."
    assert got["concept"] == "The coconut is never found on screen."
    assert got["metadata"] == {"cover": "palm"}


def test_sparse_series_update_with_format_brief_only(client):
    sid = _seed_series(client)

    r = _put_series(client, sid, format_brief="x")

    assert r.status_code == 200, r.text
    got = _get_series(client, sid)
    assert got["format_brief"] == "x"
    assert got["name"] == "Coconut Chronicles"
    assert got["description"] == "A show about a coconut."
    assert got["concept"] == "The coconut is never found on screen."
    assert got["metadata"] == {"cover": "palm"}
    assert got["template_id"] == "reel-v3"


def test_series_update_that_names_a_field_writes_it_even_when_empty(client):
    """The v1 frontend PUTs the whole shape; what it names is written, "" and {} included."""
    sid = _seed_series(client)

    r = _put_series(client, sid, name="Renamed", description="", metadata={})

    assert r.status_code == 200, r.text
    got = _get_series(client, sid)
    assert got["name"] == "Renamed"
    assert got["description"] == ""
    assert got["metadata"] == {}
    assert got["concept"] == "The coconut is never found on screen."


def test_series_insert_without_name_is_422(client):
    sid = str(uuid.uuid4())

    r = _put_series(client, sid, description="no name here")

    assert r.status_code == 422
    assert r.json() == {"detail": "name is required for a new series"}
    assert client.get(f"/v1/series/{sid}", headers=HEADERS).status_code == 404


def test_series_insert_with_a_null_name_is_422(client):
    r = _put_series(client, str(uuid.uuid4()), name=None)

    assert r.status_code == 422
    assert r.json() == {"detail": "name is required for a new series"}


def test_series_insert_with_only_a_name_gets_empty_defaults(client):
    sid = str(uuid.uuid4())

    assert _put_series(client, sid, name="Fresh").status_code == 200

    got = _get_series(client, sid)
    assert (got["name"], got["description"], got["concept"], got["metadata"]) == ("Fresh", "", "", {})


def test_explicit_null_still_unbinds_the_series_template(client):
    sid = _seed_series(client)

    assert _put_series(client, sid, template_id=None).status_code == 200

    got = _get_series(client, sid)
    assert got["template_id"] is None
    assert got["description"] == "A show about a coconut."


# ── cast ─────────────────────────────────────────────────────────────────────


def _seed_character(client, sid: str) -> tuple[str, dict]:
    cid = str(uuid.uuid4())
    seeded = {
        "name": "Captain",
        "description": "Sun-weathered, one eyebrow.",
        "voice": "gravel and honey",
        "kind": "character",
        "anchors": [{"label": "eyepatch"}],
        "voice_media_id": str(uuid.uuid4()),
        "reference_image_media_id": str(uuid.uuid4()),
        "generator_profile_id": str(uuid.uuid4()),
        "metadata": {"portrait": "m-1"},
    }
    r = _put_character(client, cid, sid, **seeded)
    assert r.status_code == 200, r.text
    return cid, seeded


def test_sparse_character_update_keeps_picture_voice_description(client):
    sid = _seed_series(client)
    cid, seeded = _seed_character(client, sid)

    r = _put_character(client, cid, sid, anchors=[{"label": "a parrot"}])

    assert r.status_code == 200, r.text
    got = _get_character(client, cid)
    assert got["anchors"] == [{"label": "a parrot"}]
    assert got["name"] == seeded["name"]
    assert got["description"] == seeded["description"]
    assert got["voice"] == seeded["voice"]
    assert got["reference_image_media_id"] == seeded["reference_image_media_id"]
    assert got["generator_profile_id"] == seeded["generator_profile_id"]
    assert got["voice_media_id"] == seeded["voice_media_id"]
    assert got["metadata"] == seeded["metadata"]


def test_sparse_character_update_can_rename_alone(client):
    sid = _seed_series(client)
    cid, seeded = _seed_character(client, sid)

    assert _put_character(client, cid, sid, name="Skipper").status_code == 200

    got = _get_character(client, cid)
    assert got["name"] == "Skipper"
    assert got["description"] == seeded["description"]
    assert got["anchors"] == seeded["anchors"]
    assert got["metadata"] == seeded["metadata"]


def test_character_update_that_names_a_field_writes_it_even_when_empty(client):
    sid = _seed_series(client)
    cid, seeded = _seed_character(client, sid)

    assert _put_character(client, cid, sid, description="", voice="", metadata={}).status_code == 200

    got = _get_character(client, cid)
    assert (got["description"], got["voice"], got["metadata"]) == ("", "", {})
    assert got["name"] == seeded["name"]
    assert got["reference_image_media_id"] == seeded["reference_image_media_id"]


def test_character_insert_without_name_is_422(client):
    sid = _seed_series(client)
    cid = str(uuid.uuid4())

    r = _put_character(client, cid, sid, description="no name")

    assert r.status_code == 422
    assert r.json() == {"detail": "name is required for a new character"}
    assert client.get(f"/v1/characters/{cid}", headers=HEADERS).status_code == 404


def test_explicit_null_still_clears_a_nullable_id(client):
    sid = _seed_series(client)
    cid, seeded = _seed_character(client, sid)

    r = _put_character(
        client,
        cid,
        sid,
        voice_media_id=None,
        reference_image_media_id=None,
        generator_profile_id=None,
    )

    assert r.status_code == 200, r.text
    got = _get_character(client, cid)
    assert got["voice_media_id"] is None
    assert got["reference_image_media_id"] is None
    assert got["generator_profile_id"] is None
    assert got["description"] == seeded["description"]
    assert got["voice"] == seeded["voice"]
