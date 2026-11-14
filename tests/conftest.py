"""Shared fixtures: the store app over sqlite, with two users and the clip and media tables.

``env`` drives the real routes (as tests/test_series_ownership.py does) and reads
rows back straight from the database, so a test can say what a route did to a row.
App modules are imported inside the fixture: other test modules set environment
variables at import time, before the app is first imported.
"""
from __future__ import annotations

import asyncio
import logging
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateTable


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(element, compiler, **kw):  # pragma: no cover - trivial
    return "JSON"


@compiles(UUID, "sqlite")
def _uuid_sqlite(element, compiler, **kw):  # pragma: no cover - trivial
    return "VARCHAR(36)"


SECRET = "shared-env-secret"
USER_A = str(uuid.uuid4())
USER_B = str(uuid.uuid4())


def headers(user: str | None) -> dict[str, str]:
    out = {"X-Internal-Secret": SECRET}
    if user:
        out["X-User-ID"] = user
    return out


def _create_tables(sync_conn) -> None:
    from app.models import ClipPrompt, ClipstyleAsset, MediaItem, RenderTemplate, User

    for table in (User, RenderTemplate, ClipPrompt, MediaItem, ClipstyleAsset):
        ddl = str(CreateTable(table.__table__).compile(dialect=sync_conn.dialect))
        sync_conn.exec_driver_sql(ddl.replace("'{}'::jsonb", "'{}'"))
    sync_conn.exec_driver_sql("CREATE TABLE generator_profiles (id VARCHAR(36) PRIMARY KEY)")


class Env:
    A, B = USER_A, USER_B

    def __init__(self, client: TestClient, factory) -> None:
        self.client, self.factory = client, factory

    headers = staticmethod(headers)

    def seed(self, *rows) -> None:
        async def go():
            async with self.factory() as s:
                s.add_all(rows)
                await s.commit()

        asyncio.run(go())

    def row(self, clip_id: str):
        """The clip row as the database holds it (None: no such clip)."""
        return self._get("ClipPrompt", clip_id)

    def media_row(self, media_id: str):
        return self._get("MediaItem", media_id)

    def _get(self, model: str, row_id: str):
        from app import models

        async def go():
            async with self.factory() as s:
                return await s.get(getattr(models, model), row_id)

        return asyncio.run(go())

    def clip(self, user: str | None, name: str = "Original") -> str:
        """A clip owned by ``user`` (None: a legacy clip with no owner), with one image."""
        from app.models import ClipPrompt, MediaItem

        cid, mid = str(uuid.uuid4()), str(uuid.uuid4())
        self.seed(
            MediaItem(id=mid, user_id=user, type="image", prompt="old"),
            ClipPrompt(id=cid, user_id=user, name=name, media_refs={"images": [mid], "ai_videos": [], "audios": []}),
        )
        return cid

    def new_media(self, user: str | None, **columns) -> str:
        from app.models import MediaItem

        mid = str(uuid.uuid4())
        self.seed(MediaItem(id=mid, user_id=user, **{"type": "image", "prompt": "new", **columns}))
        return mid

    def images(self, clip_id: str) -> list[str]:
        return self.row(clip_id).media_refs["images"]

    def put(self, user: str | None, cid: str, **body):
        return self.client.put(f"/v1/clips/{cid}", json={"id": cid, **body}, headers=headers(user))

    def patch(self, user: str | None, cid: str, **body):
        return self.client.patch(f"/v1/clips/{cid}", json=body, headers=headers(user))

    def swap(self, user: str | None, cid: str, old: str, new: str):
        body = {"kind": "image", "media_index": 0, "old_media_id": old, "new_media_id": new}
        return self.client.post(f"/v1/clips/{cid}/swap", json=body, headers=headers(user))


@pytest.fixture()
def env(tmp_path, monkeypatch):
    from app.db import get_session
    from app.fastapi_app import create_fastapi_app
    from app.models import User

    monkeypatch.setenv("INTERNAL_API_SECRET", SECRET)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'env.db'}", poolclass=NullPool)

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


@pytest.fixture()
def restore_logging():
    """init_logging and alembic's fileConfig rewrite process-wide logging: put it back afterwards."""
    root = logging.getLogger()
    saved_root = (list(root.handlers), root.level)
    saved = {
        name: (list(lg.handlers), lg.level, lg.propagate, lg.disabled)
        for name, lg in logging.root.manager.loggerDict.items()
        if isinstance(lg, logging.Logger)
    }
    yield
    root.handlers[:], root.level = saved_root[0], saved_root[1]
    for name, (handlers, level, propagate, disabled) in saved.items():
        lg = logging.getLogger(name)
        lg.handlers[:], lg.level, lg.propagate, lg.disabled = handlers, level, propagate, disabled


@pytest.fixture()
def restore_migration_status():
    """run_migrations records its outcome in a process-wide variable that /healthz reports."""
    from app.migration_state import get_migration_status, set_migration_status

    saved = get_migration_status()
    yield
    set_migration_status(saved)
