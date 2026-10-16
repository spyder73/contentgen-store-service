"""Phase 4 brand assets in the store: config.assets, the hidden asset kind, freeze at approval
(migration 0039). Store tests run on in-memory sqlite like test_clipstyle_render.py."""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.schema import CreateTable


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(element, compiler, **kw):  # pragma: no cover - trivial
    return "JSON"


@compiles(UUID, "sqlite")
def _uuid_sqlite(element, compiler, **kw):  # pragma: no cover - trivial
    return "VARCHAR(36)"


from app.models import ClipstyleAsset, MediaItem, RenderTemplate  # noqa: E402
from app.stores import clipstyle_assets as asset_store  # noqa: E402
from app.stores import clipstyle_packages as cp  # noqa: E402
from app.stores import media as media_store  # noqa: E402

OWNER = "00000000-0000-0000-0000-000000000001"
OTHER = "00000000-0000-0000-0000-000000000002"
M1 = "11111111-1111-1111-1111-111111111111"
SHA = "c" * 64
FILES = {"manifest.ts": "m"}
OK = {"ok": True, "contract_version": 1, "package": {"sha256": cp.package_sha256(FILES)}}


def _config(**assets):
    return {"manifest": {"id": "neonKit", "version": 1, "kind": "carousel"}, "files": FILES, "assets": assets}


def test_config_assets_shape():
    cp.check_package_config(_config(logo={"sha256": SHA, "media_id": M1, "mime": "image/png"}))
    for bad in ({"sticker": {"sha256": SHA, "media_id": M1, "mime": "image/png"}},
                {"logo": {"sha256": "x", "media_id": M1, "mime": "image/png"}},
                {"logo": {"sha256": SHA, "media_id": M1, "mime": "image/svg+xml"}},
                {"logo": {"sha256": SHA, "media_id": "", "mime": "image/png"}},
                {"logo": {"sha256": SHA, "media_id": "m-1", "mime": "image/png"}}):
        with pytest.raises(ValueError, match="invalid_package_config:assets"):
            cp.check_package_config(_config(**bad))
    with pytest.raises(ValueError, match="invalid_package_config:assets"):
        cp.check_package_config({**_config(), "assets": ["logo"]})


def _create_tables(sync_conn):
    for table in (RenderTemplate.__table__, MediaItem.__table__, ClipstyleAsset.__table__):
        ddl = str(CreateTable(table).compile(dialect=sync_conn.dialect))
        sync_conn.exec_driver_sql(ddl.replace("'{}'::jsonb", "'{}'"))


def _run(objects, fn):
    async def go():
        engine = create_async_engine("sqlite+aiosqlite://")
        async with engine.begin() as conn:
            await conn.run_sync(_create_tables)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as s:
            s.add_all(objects)
            await s.commit()
        try:
            async with factory() as s:
                return await fn(s)
        finally:
            await engine.dispose()

    return asyncio.run(go())


def _row(status="validated", **assets):
    now = datetime(2026, 10, 10, tzinfo=timezone.utc)
    return RenderTemplate(id="tpl-1", user_id=OWNER, name="n", kind="clipstyle_package", source="agent_generated", status=status,
                          visibility="private", config=_config(**assets), validation_report_json=OK, created_at=now, updated_at=now)


def _media(id_=M1, user_id=OWNER, kind="clipstyle_asset"):
    return MediaItem(id=id_, user_id=user_id, type="image", metadata_={"kind": kind, "label": "logo"})


LOGO = {"sha256": SHA, "media_id": M1, "mime": "image/png"}


def test_approval_freezes_the_config_assets():
    async def fn(s):
        await cp.set_package_status(s, "tpl-1", "approved", user_id=OWNER)
        return [(a.label, a.sha256, a.media_id, a.mime) for a in await asset_store.list_assets(s, "tpl-1")]

    assert _run([_row(logo=LOGO), _media()], fn) == [("logo", SHA, M1, "image/png")]


@pytest.mark.parametrize("media", [[], [_media(user_id=OTHER)], [_media(kind="upload")]])
def test_approval_refuses_an_asset_that_is_not_the_owners_hidden_asset(media):
    async def fn(s):
        with pytest.raises(ValueError, match="package_asset_missing"):
            await cp.set_package_status(s, "tpl-1", "approved", user_id=OWNER)
        await s.rollback()
        return (await s.get(RenderTemplate, "tpl-1")).status

    assert _run([_row(logo=LOGO), *media], fn) == "validated"


def test_a_frozen_asset_cannot_change_or_be_deleted():
    async def fn(s):
        await cp.set_package_status(s, "tpl-1", "approved", user_id=OWNER)
        with pytest.raises(ValueError, match="media_frozen_in_clipstyle"):
            await media_store.delete_media(s, M1, user_id=OWNER)
        return len(await asset_store.list_assets(s, "tpl-1"))

    assert _run([_row(logo=LOGO), _media()], fn) == 1


def test_asset_media_are_hidden_from_listings():
    assert set(media_store.HIDDEN_MEDIA_KINDS) == {"clipstyle_preview", "clipstyle_bundle", "clipstyle_asset"}


def test_migration_0039_sql_has_the_table_trigger_and_guarded_downgrade():
    root = Path(__file__).resolve().parent.parent
    env = {**os.environ, "DATABASE_URL": "postgresql+asyncpg://u:p@localhost/x"}
    up = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "0038:0039", "--sql"],
                        cwd=root, env=env, capture_output=True, text=True, check=True).stdout
    assert "CREATE TABLE clipstyle_assets" in up and "clipstyle_assets_append_only" in up
    assert "label IN ('logo','wordmark','texture','background','icon')" in up
    down = subprocess.run([sys.executable, "-m", "alembic", "downgrade", "0039:0038", "--sql"],
                          cwd=root, env=env, capture_output=True, text=True, check=True).stdout
    assert "downgrade 0039 refused" in down
