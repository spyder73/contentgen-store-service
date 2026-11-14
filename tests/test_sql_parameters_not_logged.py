"""No SQL parameter reaches a log line.

With the logging fixed (see test_unhandled_error_log.py) `logger.exception("unhandled error: %s", exc)`
is live again, and a SQLAlchemy error's text includes the statement's bound parameters (prompts,
file_url, metadata, usernames, password hashes) unless the engine hides them. Every engine of the
store is made by app.db.make_engine, which does.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db import make_engine
from app.logging_config import init_logging
from tests.conftest import SECRET, USER_A, USER_B, _create_tables, headers

STORE_ROOT = Path(__file__).resolve().parents[1]
BOUND_VALUE = "SECRET-PARAM-123"


def _with_foreign_keys(engine):
    @event.listens_for(engine.sync_engine, "connect")
    def _on(dbapi_conn, _record):  # pragma: no cover - trivial
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


def _refused_insert():
    """An INSERT the database refuses (no such clip), carrying BOUND_VALUE as a bound parameter."""
    from app.models import MediaItem

    return insert(MediaItem).values(
        id=str(uuid.uuid4()), user_id=USER_A, type="image", prompt=BOUND_VALUE, clip_id=str(uuid.uuid4())
    )


def _sqlite_url(tmp_path) -> str:
    return f"sqlite+aiosqlite:///{tmp_path / 'hygiene.db'}"


@pytest.fixture()
def store_db(tmp_path):
    """A sqlite store database with foreign keys on, and the two users."""
    from app.models import User

    url = _sqlite_url(tmp_path)
    engine = _with_foreign_keys(create_async_engine(url, poolclass=NullPool))

    async def setup():
        async with engine.begin() as conn:
            await conn.run_sync(_create_tables)
        async with async_sessionmaker(engine, expire_on_commit=False)() as s:
            s.add_all([User(id=USER_A, username="a", password_hash="x"), User(id=USER_B, username="b", password_hash="x")])
            await s.commit()
        await engine.dispose()

    asyncio.run(setup())
    return url


def _lines(stdout: str, msg_prefix: str) -> list[dict]:
    lines = [json.loads(line) for line in stdout.splitlines() if line.startswith("{")]
    return [line for line in lines if line["msg"].startswith(msg_prefix)]


def test_an_unhandled_sql_error_logs_no_bound_value(store_db, monkeypatch, capsys, restore_logging):
    from app.db import get_session
    from app.fastapi_app import create_fastapi_app
    from app.stores import media as media_store

    # Control: the same statement on an engine that does not hide parameters does leak the value,
    # so the assertions below mean something.
    async def refused_on(engine):
        async with async_sessionmaker(engine)() as s:
            await s.execute(_refused_insert())

    plain = _with_foreign_keys(create_async_engine(store_db, poolclass=NullPool))
    with pytest.raises(IntegrityError) as leaky:
        asyncio.run(refused_on(plain))
    assert BOUND_VALUE in str(leaky.value)

    # The app's engine, made the way the store makes every engine.
    engine = _with_foreign_keys(make_engine(store_db, poolclass=NullPool))
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def session():
        async with factory() as s:
            yield s

    async def get_media_that_fails(session, media_id, user_id=None):
        await session.execute(_refused_insert())

    monkeypatch.setattr(media_store, "get_media", get_media_that_fails)
    monkeypatch.setenv("INTERNAL_API_SECRET", SECRET)
    app = create_fastapi_app()
    app.dependency_overrides[get_session] = session
    init_logging("store")  # after capsys: the handler binds the captured stdout

    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get(f"/v1/media/{uuid.uuid4()}", headers=headers(USER_A))
    asyncio.run(engine.dispose())

    assert (resp.status_code, resp.json()) == (500, {"error": "internal_server_error"})
    out = capsys.readouterr().out
    [line] = _lines(out, "unhandled error")
    assert "IntegrityError" in line["exc"] and "FOREIGN KEY" in line["msg"]  # still says what failed
    assert BOUND_VALUE not in line["msg"]
    assert BOUND_VALUE not in line["exc"]
    assert BOUND_VALUE not in out


def test_every_engine_of_the_store_hides_parameters():
    assert make_engine("sqlite+aiosqlite://").sync_engine.hide_parameters is True
    # No other way to make an engine in the app or in alembic/env.py (the migration step's own engine).
    for path in [*(STORE_ROOT / "app").rglob("*.py"), STORE_ROOT / "alembic" / "env.py"]:
        if path.name == "db.py" and path.parent.name == "app":
            continue
        assert "create_async_engine(" not in path.read_text(), f"{path} makes its own engine: use app.db.make_engine"
