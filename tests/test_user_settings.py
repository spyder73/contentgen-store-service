"""Per-user settings blob: routes + store semantics.

Same harness as the other route tests -- a TestClient over the real FastAPI
app with a MagicMock session, and store-level round-trips against a mocked
session that exercises the real store wiring.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.db import get_session
from app.fastapi_app import create_fastapi_app
from app.models import User, UserSetting
from app.stores import user_settings

SECRET = "settings-test-secret"
USER_ID = "00000000-0000-0000-0000-0000000000aa"
OTHER_ID = "00000000-0000-0000-0000-0000000000bb"


async def _session_dep():
    yield MagicMock()


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_SECRET", SECRET)
    app = create_fastapi_app()
    app.dependency_overrides[get_session] = _session_dep
    with TestClient(app) as value:
        yield value


def _headers():
    return {"X-Internal-Secret": SECRET}


# ── routes ──────────────────────────────────────────────────────────────────


def test_settings_routes_require_internal_secret(client):
    assert client.get(f"/v1/user-settings/{USER_ID}").status_code == 401
    assert client.put(f"/v1/user-settings/{USER_ID}", json={}).status_code == 401
    assert client.patch(f"/v1/user-settings/{USER_ID}", json={}).status_code == 401


def test_get_returns_empty_object_when_no_row(client):
    with patch(
        "app.stores.user_settings.get_user_settings", new=AsyncMock(return_value={})
    ) as call:
        response = client.get(f"/v1/user-settings/{USER_ID}", headers=_headers())
    assert response.status_code == 200
    assert response.json() == {}
    assert call.call_args.args[1] == USER_ID


def test_get_returns_stored_object(client):
    stored = {"reviewer": {"default_review_mode": "on", "confidence_gate": 0.9}}
    with patch("app.stores.user_settings.get_user_settings", new=AsyncMock(return_value=stored)):
        response = client.get(f"/v1/user-settings/{USER_ID}", headers=_headers())
    assert response.status_code == 200
    assert response.json() == stored


def test_put_passes_full_object_through_and_returns_stored(client):
    body = {"reviewer": {"max_attempts": 3}, "ui": {"theme": "dark"}}
    with patch(
        "app.stores.user_settings.put_user_settings", new=AsyncMock(return_value=body)
    ) as call:
        response = client.put(f"/v1/user-settings/{USER_ID}", headers=_headers(), json=body)
    assert response.status_code == 200
    assert response.json() == body
    assert call.call_args.args[1] == USER_ID
    assert call.call_args.args[2] == body


def test_patch_returns_merged_object(client):
    merged = {"reviewer": {"max_attempts": 3}, "ui": {"theme": "dark"}}
    with patch(
        "app.stores.user_settings.patch_user_settings", new=AsyncMock(return_value=merged)
    ) as call:
        response = client.patch(
            f"/v1/user-settings/{USER_ID}", headers=_headers(), json={"ui": {"theme": "dark"}}
        )
    assert response.status_code == 200
    assert response.json() == merged
    assert call.call_args.args[2] == {"ui": {"theme": "dark"}}


@pytest.mark.parametrize("method", ["put", "patch"])
@pytest.mark.parametrize("body", [["not", "an", "object"], "string", 7, None])
def test_non_object_body_is_400(client, method, body):
    response = getattr(client, method)(
        f"/v1/user-settings/{USER_ID}", headers=_headers(), json=body
    )
    assert response.status_code == 400


@pytest.mark.parametrize("method", ["put", "patch"])
def test_unknown_user_is_404(client, method):
    error = user_settings.UserSettingsError(404, "user_not_found")
    target = f"app.stores.user_settings.{method}_user_settings"
    with patch(target, new=AsyncMock(side_effect=error)):
        response = getattr(client, method)(
            f"/v1/user-settings/{OTHER_ID}", headers=_headers(), json={"a": 1}
        )
    assert response.status_code == 404
    assert response.json()["detail"] == "user_not_found"


# ── store round-trips (mocked session, exercises the real store wiring) ─────


def _store_session(*, user_exists: bool = True, row: UserSetting | None = None):
    session = MagicMock()
    added: list = []

    async def _get(model, key):
        if model is User:
            return User(id=key) if user_exists else None
        return row

    session.get = AsyncMock(side_effect=_get)
    session.add = MagicMock(side_effect=added.append)
    session.commit = AsyncMock()
    session.refresh = AsyncMock()
    session.added = added
    return session


@pytest.mark.asyncio
async def test_get_missing_row_reads_as_empty_object():
    session = _store_session(row=None)
    assert await user_settings.get_user_settings(session, USER_ID) == {}


@pytest.mark.asyncio
async def test_get_returns_a_copy_of_the_stored_object():
    row = UserSetting(user_id=USER_ID, settings={"reviewer": {"max_attempts": 2}})
    session = _store_session(row=row)

    out = await user_settings.get_user_settings(session, USER_ID)

    assert out == {"reviewer": {"max_attempts": 2}}
    out["reviewer"] = "clobbered"
    assert row.settings == {"reviewer": {"max_attempts": 2}}


@pytest.mark.asyncio
async def test_put_inserts_a_row_when_none_exists():
    session = _store_session(row=None)

    out = await user_settings.put_user_settings(session, USER_ID, {"ui": {"theme": "dark"}})

    assert out == {"ui": {"theme": "dark"}}
    assert session.added[0].user_id == USER_ID
    assert session.added[0].settings == {"ui": {"theme": "dark"}}
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_put_replaces_the_whole_object():
    row = UserSetting(user_id=USER_ID, settings={"reviewer": {"max_attempts": 2}, "ui": {}})
    session = _store_session(row=row)

    out = await user_settings.put_user_settings(session, USER_ID, {"ui": {"theme": "dark"}})

    assert out == {"ui": {"theme": "dark"}}
    assert row.settings == {"ui": {"theme": "dark"}}
    assert session.added == []


@pytest.mark.asyncio
async def test_patch_merges_top_level_keys_and_leaves_others():
    row = UserSetting(
        user_id=USER_ID,
        settings={"reviewer": {"max_attempts": 2}, "ui": {"theme": "light"}},
    )
    session = _store_session(row=row)

    out = await user_settings.patch_user_settings(
        session, USER_ID, {"ui": {"theme": "dark"}, "new": 1}
    )

    assert out == {
        "reviewer": {"max_attempts": 2},
        "ui": {"theme": "dark"},
        "new": 1,
    }
    assert row.settings == out


@pytest.mark.asyncio
async def test_patch_overwrites_a_section_wholesale():
    row = UserSetting(user_id=USER_ID, settings={"reviewer": {"max_attempts": 2, "gate": 0.85}})
    session = _store_session(row=row)

    out = await user_settings.patch_user_settings(
        session, USER_ID, {"reviewer": {"max_attempts": 3}}
    )

    # top-level merge only: the section is replaced, not deep-merged
    assert out == {"reviewer": {"max_attempts": 3}}


@pytest.mark.asyncio
async def test_patch_inserts_a_row_when_none_exists():
    session = _store_session(row=None)

    out = await user_settings.patch_user_settings(session, USER_ID, {"reviewer": {"mode": "on"}})

    assert out == {"reviewer": {"mode": "on"}}
    assert session.added[0].settings == {"reviewer": {"mode": "on"}}


@pytest.mark.asyncio
@pytest.mark.parametrize("func", ["put_user_settings", "patch_user_settings"])
async def test_unknown_user_raises_404(func):
    session = _store_session(user_exists=False, row=None)

    with pytest.raises(user_settings.UserSettingsError) as exc:
        await getattr(user_settings, func)(session, OTHER_ID, {"a": 1})

    assert exc.value.status_code == 404
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("func", ["put_user_settings", "patch_user_settings"])
@pytest.mark.parametrize("body", [["a"], "a", 1, None])
async def test_non_object_payload_raises_400_before_touching_the_session(func, body):
    session = _store_session(row=None)

    with pytest.raises(user_settings.UserSettingsError) as exc:
        await getattr(user_settings, func)(session, USER_ID, body)

    assert exc.value.status_code == 400
    session.get.assert_not_awaited()
    session.commit.assert_not_awaited()


# ── migration ───────────────────────────────────────────────────────────────


def _load_migration_0031_module():
    path = Path(__file__).resolve().parent.parent / "alembic" / "versions" / "0031_user_settings.py"
    spec = importlib.util.spec_from_file_location("migration_0031_user_settings", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_0031_chains_onto_0030():
    module = _load_migration_0031_module()
    assert module.revision == "0031"
    assert module.down_revision == "0030"


def test_migration_0031_renders_the_contracted_postgres_ddl():
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy.dialects import postgresql

    module = _load_migration_0031_module()
    statements: list[str] = []
    ctx = MigrationContext.configure(
        dialect=postgresql.dialect(),
        opts={"as_sql": True, "output_buffer": _Buffer(statements)},
    )
    with Operations.context(ctx):
        module.upgrade()
        module.downgrade()

    sql = " ".join(statements)
    assert "CREATE TABLE user_settings" in sql
    assert "user_id UUID NOT NULL" in sql
    assert "settings JSONB DEFAULT \'{}\'::jsonb NOT NULL" in sql
    assert "ON DELETE CASCADE" in sql
    assert "DROP TABLE user_settings" in sql


class _Buffer:
    """Collects alembic's offline DDL instead of printing it to stdout."""

    def __init__(self, sink: list[str]):
        self._sink = sink

    def write(self, text: str) -> None:
        self._sink.append(text)

    def flush(self) -> None:
        pass
