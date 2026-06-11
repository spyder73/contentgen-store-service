"""Tests for the stamp-aware, fail-soft startup migration flow.

The production database predates alembic-managed deploys: its schema exists but
alembic_version does not. A plain `upgrade head` replays migration 0001 onto
existing tables and crash-loops the container, so startup must:
  - classify the database (stamped / legacy / empty),
  - stamp the legacy baseline before upgrading an unmanaged schema, and
  - NEVER propagate a migration failure (serve anyway, report on /healthz).
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app import main as app_main
from app.db import get_session
from app.fastapi_app import create_fastapi_app
from app.migration_state import get_migration_status, set_migration_status


async def _mock_session():
    yield MagicMock()


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_SECRET", "test-secret-xyz")
    app = create_fastapi_app()
    app.dependency_overrides[get_session] = _mock_session
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


def _sqlite_url(tmp_path, name: str) -> str:
    return f"sqlite+aiosqlite:///{tmp_path / name}"


async def _exec(url: str, statement: str) -> None:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text(statement))
    finally:
        await engine.dispose()


class TestDatabaseState:
    def test_empty_database(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", _sqlite_url(tmp_path, "empty.db"))
        assert app_main._database_state() == "empty"

    def test_legacy_database_has_tables_but_no_alembic_version(self, tmp_path, monkeypatch):
        import asyncio

        url = _sqlite_url(tmp_path, "legacy.db")
        asyncio.run(_exec(url, "CREATE TABLE prompt_templates (id TEXT PRIMARY KEY)"))
        monkeypatch.setenv("DATABASE_URL", url)
        assert app_main._database_state() == "legacy"

    def test_stamped_database(self, tmp_path, monkeypatch):
        import asyncio

        url = _sqlite_url(tmp_path, "stamped.db")
        asyncio.run(_exec(url, "CREATE TABLE alembic_version (version_num TEXT)"))
        monkeypatch.setenv("DATABASE_URL", url)
        assert app_main._database_state() == "stamped"


class TestRunMigrations:
    def test_skips_without_database_url(self, monkeypatch):
        monkeypatch.delenv("DATABASE_URL", raising=False)
        set_migration_status("not run")
        app_main.run_migrations()
        assert get_migration_status() == "skipped (no DATABASE_URL)"

    def test_legacy_database_is_stamped_before_upgrade(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://x:y@nowhere/db")
        calls = []
        with patch.object(app_main, "_database_state", return_value="legacy"), \
             patch("alembic.command.stamp", side_effect=lambda cfg, rev: calls.append(("stamp", rev))), \
             patch("alembic.command.upgrade", side_effect=lambda cfg, rev: calls.append(("upgrade", rev))):
            set_migration_status("not run")
            app_main.run_migrations()
        assert calls == [("stamp", app_main._LEGACY_BASELINE), ("upgrade", "head")]
        assert get_migration_status() == "ok"

    def test_stamped_database_upgrades_without_stamping(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://x:y@nowhere/db")
        calls = []
        with patch.object(app_main, "_database_state", return_value="stamped"), \
             patch("alembic.command.stamp", side_effect=lambda cfg, rev: calls.append(("stamp", rev))), \
             patch("alembic.command.upgrade", side_effect=lambda cfg, rev: calls.append(("upgrade", rev))):
            set_migration_status("not run")
            app_main.run_migrations()
        assert calls == [("upgrade", "head")]
        assert get_migration_status() == "ok"

    def test_failure_is_swallowed_and_reported(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://x:y@nowhere/db")
        with patch.object(
            app_main, "_database_state", side_effect=RuntimeError("relation already exists")
        ):
            set_migration_status("not run")
            app_main.run_migrations()  # must not raise
        status = get_migration_status()
        assert status.startswith("failed: RuntimeError")
        assert "relation already exists" in status


class TestHealthz:
    def test_healthz_reports_migration_status(self, client):
        set_migration_status("ok")
        body = client.get("/healthz").json()
        assert body == {"status": "ok", "migrations": "ok"}

        set_migration_status("failed: RuntimeError: boom")
        body = client.get("/healthz").json()
        assert body["migrations"].startswith("failed:")
