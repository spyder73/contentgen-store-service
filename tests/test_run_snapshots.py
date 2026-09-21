from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.db import get_session
from app.fastapi_app import create_fastapi_app
from app.schemas import PipelineRunSnapshotOut

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


def _headers(user_id: str) -> dict[str, str]:
    return {"X-User-ID": user_id, "X-Internal-Secret": INTERNAL_SECRET}


def _id(n: int) -> str:
    return f"00000000-0000-0000-0000-{n:012d}"


def _snapshot_out(id_: str, user_id: str | None, status: str = "completed", **overrides) -> PipelineRunSnapshotOut:
    now = datetime.now(timezone.utc)
    data = {
        "id": id_,
        "user_id": user_id,
        "status": status,
        "snapshot": {"id": id_, "status": status, "user_id": user_id},
        "created_at": now,
        "updated_at": now,
    }
    data.update(overrides)
    return PipelineRunSnapshotOut(**data)


class TestUpsertRunSnapshot:
    def test_upsert_persists_body_and_path_id(self, client):
        uid = _id(1)
        rid = _id(101)
        expected = _snapshot_out(rid, uid, status="running")
        with patch(
            "app.stores.run_snapshots.upsert_snapshot",
            new=AsyncMock(return_value=expected),
        ) as mock_upsert:
            resp = client.put(
                f"/v1/run-snapshots/{rid}",
                json={"id": "ignored-by-handler", "status": "running", "snapshot": {"status": "running"}},
                headers=_headers(uid),
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == rid
        assert body["status"] == "running"
        # Handler overrides body.id with the path id and forwards the header user.
        _, kwargs = mock_upsert.call_args
        sent_body = mock_upsert.call_args[0][1]
        assert sent_body.id == rid
        assert kwargs["user_id"] == uid

    def test_upsert_without_user_header_is_allowed(self, client):
        rid = _id(102)
        expected = _snapshot_out(rid, None, status="failed")
        with patch(
            "app.stores.run_snapshots.upsert_snapshot",
            new=AsyncMock(return_value=expected),
        ) as mock_upsert:
            resp = client.put(
                f"/v1/run-snapshots/{rid}",
                json={"id": rid, "status": "failed", "snapshot": {"status": "failed"}},
                headers={"X-Internal-Secret": INTERNAL_SECRET},
            )
        assert resp.status_code == 200
        assert mock_upsert.call_args[1]["user_id"] is None


class TestGetRunSnapshot:
    def test_get_existing(self, client):
        rid = _id(201)
        expected = _snapshot_out(rid, _id(1), status="paused")
        with patch(
            "app.stores.run_snapshots.get_snapshot",
            new=AsyncMock(return_value=expected),
        ):
            resp = client.get(f"/v1/run-snapshots/{rid}", headers=_headers(_id(1)))
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == rid
        assert body["status"] == "paused"
        assert body["snapshot"]["status"] == "paused"

    def test_get_missing_returns_404(self, client):
        rid = _id(202)
        with patch(
            "app.stores.run_snapshots.get_snapshot",
            new=AsyncMock(return_value=None),
        ):
            resp = client.get(f"/v1/run-snapshots/{rid}", headers=_headers(_id(1)))
        assert resp.status_code == 404


class TestListRunSnapshots:
    def test_list_forwards_header_user(self, client):
        uid = _id(1)
        items = [
            _snapshot_out(_id(301), uid, status="completed"),
            _snapshot_out(_id(302), uid, status="failed"),
        ]
        with patch(
            "app.stores.run_snapshots.list_snapshots",
            new=AsyncMock(return_value=items),
        ) as mock_list:
            resp = client.get("/v1/run-snapshots", headers=_headers(uid))
        assert resp.status_code == 200
        body = resp.json()
        assert [s["id"] for s in body] == [_id(301), _id(302)]
        assert mock_list.call_args[1]["user_id"] == uid


class TestDeleteRunSnapshot:
    def test_delete_existing_removes_row(self, client):
        rid = _id(401)
        with patch(
            "app.stores.run_snapshots.delete_snapshot",
            new=AsyncMock(return_value=True),
        ) as mock_delete:
            resp = client.delete(f"/v1/run-snapshots/{rid}", headers=_headers(_id(1)))
        assert resp.status_code == 204
        assert mock_delete.call_args[0][1] == rid

        # Subsequent GET reflects the row is gone.
        with patch(
            "app.stores.run_snapshots.get_snapshot",
            new=AsyncMock(return_value=None),
        ):
            resp = client.get(f"/v1/run-snapshots/{rid}", headers=_headers(_id(1)))
        assert resp.status_code == 404

    def test_delete_missing_is_idempotent(self, client):
        rid = _id(402)
        with patch(
            "app.stores.run_snapshots.delete_snapshot",
            new=AsyncMock(return_value=False),
        ):
            resp = client.delete(f"/v1/run-snapshots/{rid}", headers=_headers(_id(1)))
        assert resp.status_code == 204

    def test_delete_twice_is_idempotent(self, client):
        rid = _id(403)
        with patch(
            "app.stores.run_snapshots.delete_snapshot",
            new=AsyncMock(side_effect=[True, False]),
        ) as mock_delete:
            first = client.delete(f"/v1/run-snapshots/{rid}", headers=_headers(_id(1)))
            second = client.delete(f"/v1/run-snapshots/{rid}", headers=_headers(_id(1)))
        assert first.status_code == 204
        assert second.status_code == 204
        assert mock_delete.call_count == 2


class TestBulkDeleteRunSnapshots:
    def test_bulk_delete_returns_count(self, client):
        with patch(
            "app.stores.run_snapshots.delete_all_snapshots",
            new=AsyncMock(return_value=7),
        ) as mock_delete_all:
            resp = client.delete("/v1/run-snapshots", headers=_headers(_id(1)))
        assert resp.status_code == 200
        assert resp.json() == {"deleted": 7}
        mock_delete_all.assert_awaited_once()

    def test_bulk_delete_when_empty_returns_zero(self, client):
        with patch(
            "app.stores.run_snapshots.delete_all_snapshots",
            new=AsyncMock(return_value=0),
        ):
            resp = client.delete("/v1/run-snapshots", headers=_headers(_id(1)))
        assert resp.status_code == 200
        assert resp.json() == {"deleted": 0}


def test_snapshot_revision_prevents_out_of_order_evidence_loss():
    """Late dispatch writes and same-revision retries cannot erase completion."""
    import asyncio
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from app.schemas import PipelineRunSnapshotIn
    from app.stores.run_snapshots import upsert_snapshot

    async def run():
        engine = create_async_engine("sqlite+aiosqlite://")
        # The production model uses PostgreSQL-only JSONB server-default syntax;
        # use equivalent SQLite DDL while exercising the real atomic DAO.
        async with engine.begin() as conn:
            await conn.execute(text("""CREATE TABLE pipeline_run_snapshots (
                id VARCHAR PRIMARY KEY, user_id VARCHAR,
                status TEXT NOT NULL DEFAULT '', snapshot JSON NOT NULL DEFAULT '{}',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )"""))
        factory = async_sessionmaker(engine, expire_on_commit=False)
        rid = _id(901)
        async def save(version, status):
            snapshot = {"evidence": status}
            if version is not None:
                snapshot["_snapshot_version"] = version
            async with factory() as session:
                return await upsert_snapshot(session, PipelineRunSnapshotIn(
                    id=rid, status=status, snapshot=snapshot,
                ))

        assert (await save(None, "legacy")).status == "legacy"
        assert (await save(12, "completed")).status == "completed"
        assert (await save(11, "dispatched")).status == "completed"
        assert (await save(12, "duplicate-retry")).status == "completed"
        assert (await save(None, "late-legacy")).status == "completed"
        updated = await save(13, "regenerated")
        assert updated.status == "regenerated"
        assert updated.snapshot["_snapshot_version"] == 13
        await engine.dispose()

    asyncio.run(run())
