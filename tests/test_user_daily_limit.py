"""Admin: PATCH /v1/users/{id}/limits (daily_spend_limit).

Same harness as the other route tests -- a TestClient over the real FastAPI
app with a MagicMock session, and store-level round-trips against a mocked
session that exercises the real store wiring.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.db import get_session
from app.fastapi_app import create_fastapi_app
from app.models import User
from app.stores import users

SECRET = "daily-limit-test-secret"
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


def test_limit_route_requires_internal_secret(client):
    response = client.patch(f"/v1/users/{USER_ID}/limits", json={"daily_spend_limit": 100})
    assert response.status_code == 401


def test_patch_returns_stored_value(client):
    stored = {"user_id": USER_ID, "daily_spend_limit": 12345}
    with patch(
        "app.stores.users.set_daily_spend_limit", new=AsyncMock(return_value=stored)
    ) as call:
        response = client.patch(
            f"/v1/users/{USER_ID}/limits",
            headers=_headers(),
            json={"daily_spend_limit": 12345},
        )
    assert response.status_code == 200
    assert response.json() == stored
    assert call.call_args.args[1] == USER_ID
    assert call.call_args.args[2] == {"daily_spend_limit": 12345}


def test_unknown_user_is_404(client):
    error = users.DailyLimitError(404, "user_not_found")
    with patch("app.stores.users.set_daily_spend_limit", new=AsyncMock(side_effect=error)):
        response = client.patch(
            f"/v1/users/{OTHER_ID}/limits",
            headers=_headers(),
            json={"daily_spend_limit": 100},
        )
    assert response.status_code == 404
    assert response.json()["detail"] == "user_not_found"


@pytest.mark.parametrize(
    "body",
    [
        {"daily_spend_limit": -1},
        {"daily_spend_limit": "100"},
        {"daily_spend_limit": 1.5},
        {"daily_spend_limit": True},
        {},
        ["not", "an", "object"],
        "string",
        7,
        None,
    ],
)
def test_invalid_body_is_400(client, body):
    error = users.DailyLimitError(400, "daily_spend_limit must be a non-negative integer")
    with patch("app.stores.users.set_daily_spend_limit", new=AsyncMock(side_effect=error)):
        response = client.patch(
            f"/v1/users/{USER_ID}/limits", headers=_headers(), json=body
        )
    assert response.status_code == 400


# ── store round-trips (mocked session, exercises the real store wiring) ─────


def _store_session(*, user_exists: bool = True):
    session = MagicMock()

    async def _get(model, key):
        if model is User:
            return User(id=key, daily_spend_limit=5000) if user_exists else None
        return None

    session.get = AsyncMock(side_effect=_get)
    session.commit = AsyncMock()
    session.refresh = AsyncMock()
    return session


@pytest.mark.asyncio
async def test_set_daily_spend_limit_updates_and_returns_value():
    session = _store_session()

    out = await users.set_daily_spend_limit(session, USER_ID, {"daily_spend_limit": 999})

    assert out == {"user_id": USER_ID, "daily_spend_limit": 999}
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_set_daily_spend_limit_allows_zero():
    session = _store_session()

    out = await users.set_daily_spend_limit(session, USER_ID, {"daily_spend_limit": 0})

    assert out == {"user_id": USER_ID, "daily_spend_limit": 0}


@pytest.mark.asyncio
async def test_unknown_user_raises_404():
    session = _store_session(user_exists=False)

    with pytest.raises(users.DailyLimitError) as exc:
        await users.set_daily_spend_limit(session, OTHER_ID, {"daily_spend_limit": 100})

    assert exc.value.status_code == 404
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"daily_spend_limit": -1},
        {"daily_spend_limit": "100"},
        {"daily_spend_limit": 1.5},
        {"daily_spend_limit": True},
        {},
        ["not", "an", "object"],
        "string",
        7,
        None,
    ],
)
async def test_invalid_payload_raises_400_before_touching_the_session(body):
    session = _store_session()

    with pytest.raises(users.DailyLimitError) as exc:
        await users.set_daily_spend_limit(session, USER_ID, body)

    assert exc.value.status_code == 400
    session.get.assert_not_awaited()
    session.commit.assert_not_awaited()
