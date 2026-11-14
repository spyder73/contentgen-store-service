"""PUT /v1/media/{id} naming a clip that has no clip_prompts row answers 409, never 500.

`media_items.clip_id` is a foreign key. The Go backend can name a clip whose row it is still
writing; Postgres refused the insert, the handler caught only ValueError, and the global handler
answered 500. The store now checks the clip first (409 clip_not_found) and turns any other
IntegrityError of the write into 409 conflict. Over sqlite with foreign keys switched on (as
tests/test_series_v2.py does), since sqlite ignores them unless asked.
"""
from __future__ import annotations

import asyncio
import logging
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from tests.conftest import SECRET, USER_A, USER_B, Env, _create_tables


@pytest.fixture()
def fk_env(tmp_path, monkeypatch):
    """conftest's `env`, with PRAGMA foreign_keys=ON on every connection."""
    from app.db import get_session
    from app.fastapi_app import create_fastapi_app
    from app.models import User

    monkeypatch.setenv("INTERNAL_API_SECRET", SECRET)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'fk.db'}", poolclass=NullPool)

    @event.listens_for(engine.sync_engine, "connect")
    def _enable_foreign_keys(dbapi_conn, _record):  # pragma: no cover - trivial
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async def setup():
        async with engine.begin() as conn:
            await conn.run_sync(_create_tables)
        async with async_sessionmaker(engine, expire_on_commit=False)() as s:
            s.add_all([User(id=USER_A, username="a", password_hash="x"), User(id=USER_B, username="b", password_hash="x")])
            await s.commit()

    asyncio.run(setup())
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def session():
        async with factory() as s:
            yield s

    app = create_fastapi_app()
    app.dependency_overrides[get_session] = session
    with TestClient(app) as client:
        yield Env(client, factory)
    asyncio.run(engine.dispose())


def _put(env, mid, **fields):
    body = {"id": mid, "type": "image", "prompt": "p", **fields}
    return env.client.put(f"/v1/media/{mid}", json=body, headers=env.headers(USER_A))


def test_the_database_enforces_the_foreign_key_here(fk_env):
    """Guard for the tests below: with the check bypassed, the write itself is refused."""
    from sqlalchemy.exc import IntegrityError

    from app.models import MediaItem

    async def go():
        async with fk_env.factory() as s:
            s.add(MediaItem(id=str(uuid.uuid4()), user_id=USER_A, type="image", clip_id=str(uuid.uuid4())))
            await s.commit()

    with pytest.raises(IntegrityError):
        asyncio.run(go())


def test_put_with_an_unknown_clip_id_is_409_clip_not_found(fk_env):
    mid = str(uuid.uuid4())
    resp = _put(fk_env, mid, clip_id=str(uuid.uuid4()))
    assert resp.status_code == 409
    assert resp.json() == {"error": "clip_not_found", "detail": "clip_not_found"}
    assert fk_env.media_row(mid) is None  # nothing was written


def test_put_with_an_existing_clip_id_still_upserts(fk_env):
    cid = fk_env.clip(USER_A)
    mid = str(uuid.uuid4())

    created = _put(fk_env, mid, clip_id=cid, prompt="first")
    assert created.status_code == 200 and created.json()["clip_id"] == cid

    updated = _put(fk_env, mid, clip_id=cid, prompt="second")
    assert updated.status_code == 200
    row = fk_env.media_row(mid)
    assert (row.clip_id, row.prompt, row.user_id) == (cid, "second", USER_A)


def test_put_without_a_clip_id_still_upserts(fk_env):
    mid = str(uuid.uuid4())
    resp = _put(fk_env, mid)
    assert resp.status_code == 200 and resp.json()["clip_id"] is None


def test_the_clip_row_arriving_later_lets_the_same_put_succeed(fk_env):
    """The backend's order: the media write comes first, the clip row after. Retried, it works."""
    cid, mid = str(uuid.uuid4()), str(uuid.uuid4())
    assert _put(fk_env, mid, clip_id=cid).status_code == 409

    from app.models import ClipPrompt

    fk_env.seed(ClipPrompt(id=cid, user_id=USER_A, name="late"))
    assert _put(fk_env, mid, clip_id=cid).status_code == 200


def test_an_integrity_error_of_the_write_is_409_conflict_and_logs_no_values(fk_env, monkeypatch, caplog):
    """The check is raced by a clip deleted before the write, and the database may refuse other
    references (parent_media_id): both are the caller's data. Here the check is bypassed so the
    database itself refuses the write."""
    from app.stores import media as media_store

    async def clip_check_passes(session, clip_id):
        return True

    monkeypatch.setattr(media_store, "_clip_exists", clip_check_passes)
    mid = str(uuid.uuid4())
    with caplog.at_level(logging.WARNING, logger="app.stores.media"):
        resp = _put(fk_env, mid, clip_id=str(uuid.uuid4()), prompt="a-prompt-that-could-be-a-secret")
    assert resp.status_code == 409
    assert resp.json() == {"error": "conflict", "detail": "conflict"}
    assert fk_env.media_row(mid) is None

    [record] = [r for r in caplog.records if r.name == "app.stores.media"]
    assert record.levelno == logging.WARNING
    assert record.media_id == mid
    assert "FOREIGN KEY" in record.db_reason  # sqlite's wording; Postgres names the constraint
    assert "a-prompt-that-could-be-a-secret" not in repr(record.__dict__)

    # The session was rolled back, so the next write on the app is served normally.
    assert _put(fk_env, str(uuid.uuid4())).status_code == 200


def test_an_unknown_parent_media_id_is_409_conflict(fk_env):
    resp = _put(fk_env, str(uuid.uuid4()), parent_media_id=str(uuid.uuid4()))
    assert resp.status_code == 409
    assert resp.json()["error"] == "conflict"


def test_patch_cannot_set_a_clip_id_so_it_has_no_clip_refusal(fk_env):
    """MediaItemPatch carries file_url and metadata_merge only; a clip_id in the body is ignored,
    so the PATCH route needs no clip check."""
    mid = fk_env.new_media(USER_A)
    resp = fk_env.client.patch(
        f"/v1/media/{mid}",
        json={"file_url": "/media/uploads/x.png", "clip_id": str(uuid.uuid4())},
        headers=fk_env.headers(USER_A),
    )
    assert resp.status_code == 200
    assert fk_env.media_row(mid).clip_id is None
