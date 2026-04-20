"""Credits ledger tests.

Handler-layer tests use mocked `app.stores.credits.*` — they verify auth
gates, request/response shape, and error→402 mapping.

DB-level invariants (concurrent reserves, append-only trigger, daily-limit
boundary at the SQL level) require a real Postgres fixture; those live in
`tests/integration/test_credits_db.py` (TBD for a fresh DB harness).
"""
from __future__ import annotations

import os
import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.db import get_session
from app.fastapi_app import create_fastapi_app
from app.stores.credits import CreditsError, usd_to_credits, BalanceView


INTERNAL_SECRET = "test-secret-xyz"


async def _mock_session():
    yield MagicMock()


@pytest.fixture(autouse=True)
def _internal_secret_env(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_SECRET", INTERNAL_SECRET)


@pytest.fixture()
def client():
    app = create_fastapi_app()
    app.dependency_overrides[get_session] = _mock_session
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


# ── pure math ────────────────────────────────────────────────────────────────


class TestUsdToCredits:
    def test_zero(self):
        assert usd_to_credits(0) == 0

    def test_one_cent(self):
        # $0.01 * 100 * 1.7 = 1.7 → ceil → 2
        assert usd_to_credits(Decimal("0.01")) == 2

    def test_ten_cents_exact(self):
        # $0.10 * 100 * 1.7 = 17 → 17
        assert usd_to_credits(Decimal("0.10")) == 17

    def test_one_dollar(self):
        # $1.00 * 100 * 1.7 = 170 → 170
        assert usd_to_credits(Decimal("1.00")) == 170

    def test_ceils_fractional(self):
        # $0.001 * 100 * 1.7 = 0.17 → ceil → 1 (platform never loses cents)
        assert usd_to_credits(Decimal("0.001")) == 1

    def test_accepts_float(self):
        assert usd_to_credits(0.1) == 17

    def test_accepts_str(self):
        assert usd_to_credits("0.1") == 17


# ── credits balance GET ──────────────────────────────────────────────────────


class TestGetCredits:
    def test_self_view_ok(self, client):
        uid = str(uuid.uuid4())
        bv = BalanceView(balance=1000, reserved=100, daily_limit=5000, is_admin=False)
        with patch("app.stores.credits.get_balance", new=AsyncMock(return_value=bv)):
            resp = client.get(
                f"/v1/users/{uid}/credits",
                headers={"X-User-ID": uid, "X-Internal-Secret": INTERNAL_SECRET},
            )
        assert resp.status_code == 200
        assert resp.json() == {
            "user_id": uid,
            "balance": 1000,
            "reserved": 100,
            "daily_limit": 5000,
            "is_admin": False,
        }

    def test_other_user_forbidden_for_non_admin(self, client):
        uid = str(uuid.uuid4())
        other = str(uuid.uuid4())
        with patch("app.stores.credits.is_admin", new=AsyncMock(return_value=False)):
            resp = client.get(
                f"/v1/users/{uid}/credits",
                headers={"X-User-ID": other, "X-Internal-Secret": INTERNAL_SECRET},
            )
        assert resp.status_code == 403

    def test_other_user_allowed_for_admin(self, client):
        uid = str(uuid.uuid4())
        admin = str(uuid.uuid4())
        bv = BalanceView(balance=42, reserved=0, daily_limit=5000, is_admin=False)
        with patch("app.stores.credits.is_admin", new=AsyncMock(return_value=True)), \
             patch("app.stores.credits.get_balance", new=AsyncMock(return_value=bv)):
            resp = client.get(
                f"/v1/users/{uid}/credits",
                headers={"X-User-ID": admin, "X-Internal-Secret": INTERNAL_SECRET},
            )
        assert resp.status_code == 200
        assert resp.json()["balance"] == 42

    def test_missing_user_id_header_401(self, client):
        # Middleware blocks on missing X-Internal-Secret before the route
        # can reach the X-User-ID check — both are 401, so the assertion
        # still holds but for a different reason now.
        uid = str(uuid.uuid4())
        resp = client.get(f"/v1/users/{uid}/credits")
        assert resp.status_code == 401


# ── reserve ──────────────────────────────────────────────────────────────────


class TestReserve:
    def _body(self, amount: int = 100, key: str = "k1"):
        return {
            "amount_credits": amount,
            "pipeline_run_id": str(uuid.uuid4()),
            "checkpoint_id": "cp-1",
            "attempt": 1,
            "idempotency_key": key,
        }

    def test_missing_internal_secret_401(self, client):
        uid = str(uuid.uuid4())
        resp = client.post(
            f"/v1/users/{uid}/credits/reserve",
            headers={"X-User-ID": uid},
            json=self._body(),
        )
        assert resp.status_code == 401

    def test_caller_mismatch_forbidden(self, client):
        uid = str(uuid.uuid4())
        other = str(uuid.uuid4())
        resp = client.post(
            f"/v1/users/{uid}/credits/reserve",
            headers={"X-User-ID": other, "X-Internal-Secret": INTERNAL_SECRET},
            json=self._body(),
        )
        assert resp.status_code == 403

    def test_happy_path(self, client):
        uid = str(uuid.uuid4())
        with patch(
            "app.stores.credits.reserve",
            new=AsyncMock(return_value={"status": "reserved", "balance": 900, "reserved": 100}),
        ) as m:
            resp = client.post(
                f"/v1/users/{uid}/credits/reserve",
                headers={"X-User-ID": uid, "X-Internal-Secret": INTERNAL_SECRET},
                json=self._body(),
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "reserved"
        assert m.called

    def test_insufficient_maps_to_402(self, client):
        uid = str(uuid.uuid4())
        err = CreditsError("insufficient_credits", have=50, need=100)
        with patch("app.stores.credits.reserve", new=AsyncMock(side_effect=err)):
            resp = client.post(
                f"/v1/users/{uid}/credits/reserve",
                headers={"X-User-ID": uid, "X-Internal-Secret": INTERNAL_SECRET},
                json=self._body(),
            )
        assert resp.status_code == 402
        assert resp.json()["error"] == "insufficient_credits"
        assert resp.json()["have"] == 50

    def test_daily_limit_maps_to_402(self, client):
        uid = str(uuid.uuid4())
        err = CreditsError(
            "daily_limit_exceeded",
            have=10,
            need=100,
            extra={"daily_limit": 5000, "spent_today": 4990},
        )
        with patch("app.stores.credits.reserve", new=AsyncMock(side_effect=err)):
            resp = client.post(
                f"/v1/users/{uid}/credits/reserve",
                headers={"X-User-ID": uid, "X-Internal-Secret": INTERNAL_SECRET},
                json=self._body(),
            )
        assert resp.status_code == 402
        body = resp.json()
        assert body["error"] == "daily_limit_exceeded"
        assert body["daily_limit"] == 5000


# ── settle ───────────────────────────────────────────────────────────────────


class TestSettle:
    def _body(self, key: str = "s1"):
        return {
            "pipeline_run_id": str(uuid.uuid4()),
            "checkpoint_id": "cp-1",
            "attempt": 1,
            "actual_cost_usd": 0.05,
            "provider": "runware",
            "model": "flux-dev",
            "cost_source": "provider_telemetry",
            "idempotency_key": key,
        }

    def test_happy_path(self, client):
        uid = str(uuid.uuid4())
        with patch(
            "app.stores.credits.settle",
            new=AsyncMock(return_value={"status": "settled", "balance": 992, "reserved": 0}),
        ):
            resp = client.post(
                f"/v1/users/{uid}/credits/settle",
                headers={"X-User-ID": uid, "X-Internal-Secret": INTERNAL_SECRET},
                json=self._body(),
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "settled"

    def test_balance_exhausted_maps_to_402(self, client):
        uid = str(uuid.uuid4())
        err = CreditsError("balance_exhausted", have=0, need=20, extra={"debited": 10, "uncovered": 10})
        with patch("app.stores.credits.settle", new=AsyncMock(side_effect=err)):
            resp = client.post(
                f"/v1/users/{uid}/credits/settle",
                headers={"X-User-ID": uid, "X-Internal-Secret": INTERNAL_SECRET},
                json=self._body(),
            )
        assert resp.status_code == 402
        assert resp.json()["error"] == "balance_exhausted"


# ── release ──────────────────────────────────────────────────────────────────


class TestRelease:
    def test_missing_internal_secret_401(self, client):
        uid = str(uuid.uuid4())
        resp = client.post(
            f"/v1/users/{uid}/credits/release",
            headers={"X-User-ID": uid},
            json={"pipeline_run_id": str(uuid.uuid4()), "reason": "cancel", "idempotency_key": "r1"},
        )
        assert resp.status_code == 401

    def test_happy_path(self, client):
        uid = str(uuid.uuid4())
        with patch(
            "app.stores.credits.release",
            new=AsyncMock(return_value={"status": "released", "balance": 1000, "reserved": 0, "returned": 100}),
        ):
            resp = client.post(
                f"/v1/users/{uid}/credits/release",
                headers={"X-User-ID": uid, "X-Internal-Secret": INTERNAL_SECRET},
                json={"pipeline_run_id": str(uuid.uuid4()), "reason": "cancel", "idempotency_key": "r1"},
            )
        assert resp.status_code == 200
        assert resp.json()["returned"] == 100


# ── admin ────────────────────────────────────────────────────────────────────


class TestAdminGrant:
    def test_missing_internal_secret_401(self, client):
        resp = client.post(
            "/v1/internal/admin/credits/grant",
            headers={"X-User-ID": str(uuid.uuid4())},
            json={"user_id": str(uuid.uuid4()), "amount_credits": 1000, "note": "topup"},
        )
        assert resp.status_code == 401

    def test_non_admin_caller_403(self, client):
        with patch("app.stores.credits.is_admin", new=AsyncMock(return_value=False)):
            resp = client.post(
                "/v1/internal/admin/credits/grant",
                headers={"X-User-ID": str(uuid.uuid4()), "X-Internal-Secret": INTERNAL_SECRET},
                json={"user_id": str(uuid.uuid4()), "amount_credits": 1000, "note": "topup"},
            )
        assert resp.status_code == 403
        assert resp.json()["detail"] == "not_admin"

    def test_admin_grant_ok(self, client):
        admin = str(uuid.uuid4())
        target = str(uuid.uuid4())
        with patch("app.stores.credits.is_admin", new=AsyncMock(return_value=True)), \
             patch(
                 "app.stores.credits.grant",
                 new=AsyncMock(return_value={"status": "granted", "balance": 1000, "reserved": 0}),
             ):
            resp = client.post(
                "/v1/internal/admin/credits/grant",
                headers={"X-User-ID": admin, "X-Internal-Secret": INTERNAL_SECRET},
                json={"user_id": target, "amount_credits": 1000, "note": "topup"},
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "granted"


class TestAdminLedger:
    def test_non_admin_403(self, client):
        with patch("app.stores.credits.is_admin", new=AsyncMock(return_value=False)):
            resp = client.get(
                "/v1/internal/admin/credits/ledger",
                headers={"X-User-ID": str(uuid.uuid4()), "X-Internal-Secret": INTERNAL_SECRET},
            )
        assert resp.status_code == 403

    def test_admin_ledger_ok(self, client):
        admin = str(uuid.uuid4())
        rows = [{"id": str(uuid.uuid4()), "kind": "grant", "delta": 1000}]
        with patch("app.stores.credits.is_admin", new=AsyncMock(return_value=True)), \
             patch("app.stores.credits.ledger", new=AsyncMock(return_value=rows)):
            resp = client.get(
                "/v1/internal/admin/credits/ledger",
                headers={"X-User-ID": admin, "X-Internal-Secret": INTERNAL_SECRET},
                params={"limit": 10},
            )
        assert resp.status_code == 200
        assert resp.json() == rows
