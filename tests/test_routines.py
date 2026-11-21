"""Routines: scheduled prompts, the sweeper's claim, the daily spend counter (migration 0050).

Same harness as ``test_playbooks.py``: a sqlite database built from the models with foreign keys
switched on, so ``ON DELETE CASCADE`` is exercised for real.
"""
from __future__ import annotations

import asyncio
import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db import get_session
from app.fastapi_app import create_fastapi_app
from app.models import ChatThread, Routine, RoutineSpendDay, User
from tests.test_chat import SECRET, USER_A, USER_B, Env, _consume_body, _create_tables, _h, _render

MIGRATION_PATH = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0050_routines.py"
T0 = datetime(2026, 11, 21, 9, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


BODY = {
    "name": "Morning news",
    "prompt": "Find the top tech story.",
    "cadence": "daily",
    "time_of_day": "09:00",
    "time_zone": "Europe/London",
    "next_run_at": _iso(T0),
}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("INTERNAL_API_SECRET", SECRET)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'routines.db'}", poolclass=NullPool)

    @event.listens_for(engine.sync_engine, "connect")
    def _enable_foreign_keys(dbapi_conn, _record):  # pragma: no cover - trivial
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async def create() -> None:
        await _create_tables(engine)
        async with engine.begin() as conn:
            for table in (Routine.__table__, RoutineSpendDay.__table__):
                await conn.run_sync(lambda c, t=table: t.create(c))

    asyncio.run(create())
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _session():
        async with factory() as session:
            yield session

    app = create_fastapi_app()
    app.dependency_overrides[get_session] = _session
    with TestClient(app) as client:
        yield Env(client, factory)
    asyncio.run(engine.dispose())


def _mk(env: Env, user: str = USER_A, **over) -> dict:
    r = env.client.post(f"/v1/users/{user}/routines", json={**BODY, **over}, headers=_h(user))
    assert r.status_code == 200, r.text
    return r.json()


def _claim(env: Env, rid: str, due: datetime, nxt: datetime):
    return env.client.post(
        f"/v1/routines/{rid}/claim", json={"due_at": _iso(due), "next_run_at": _iso(nxt)}, headers=_h()
    )


def _outcome(env: Env, rid: str, outcome: str, **extra):
    return env.client.post(
        f"/v1/routines/{rid}/outcome", json={"outcome": outcome, "at": _iso(T0), **extra}, headers=_h()
    )


def test_a_sixth_routine_is_refused_with_routine_limit(env):
    for i in range(5):
        _mk(env, name=f"r{i}")
    r = env.client.post(f"/v1/users/{USER_A}/routines", json=BODY, headers=_h())
    assert r.status_code == 409 and r.json()["detail"] == "routine_limit"
    _mk(env, USER_B)  # the limit is per user


def test_the_body_is_validated(env):
    bad = env.client.post(f"/v1/users/{USER_A}/routines", json={**BODY, "cadence": "weekly"}, headers=_h())
    assert bad.status_code == 422  # weekly needs a weekday
    assert env.client.post(
        f"/v1/users/{USER_A}/routines", json={**BODY, "time_of_day": "25:00"}, headers=_h()
    ).status_code == 422
    assert _mk(env, cadence="weekly", weekday=2)["weekday"] == 2


def test_another_users_routine_is_not_found(env):
    rid = _mk(env)["id"]
    url = f"/v1/users/{USER_B}/routines/{rid}"
    assert env.client.get(url, headers=_h(USER_B)).status_code == 404
    assert env.client.patch(url, json={"state": "paused"}, headers=_h(USER_B)).status_code == 404
    assert env.client.delete(url, headers=_h(USER_B)).status_code == 404
    assert env.client.get(f"/v1/users/{USER_A}/routines/{rid}", headers=_h(USER_B)).status_code == 403
    assert env.client.get(f"/v1/users/{USER_B}/routines", headers=_h(USER_B)).json() == []
    assert env.client.get(f"/v1/users/{USER_A}/routines/{rid}", headers=_h()).json()["state"] == "active"


def test_due_lists_only_active_rows_at_or_before_now(env):
    early = _mk(env, name="early", next_run_at=_iso(T0 - timedelta(hours=1)))
    _mk(env, name="late", next_run_at=_iso(T0 + timedelta(hours=1)))
    paused = _mk(env, USER_B, name="paused", next_run_at=_iso(T0 - timedelta(hours=2)))
    env.client.patch(f"/v1/users/{USER_B}/routines/{paused['id']}", json={"state": "paused"}, headers=_h(USER_B))
    now = _iso(T0).replace("+00:00", "Z")
    r = env.client.get("/v1/routines/due", params={"now": now}, headers=_h())
    assert r.status_code == 200
    assert [x["id"] for x in r.json()] == [early["id"]]


def test_a_claim_is_won_once(env):
    row = _mk(env)
    nxt = T0 + timedelta(days=1)
    first = _claim(env, row["id"], T0, nxt)
    assert first.status_code == 200, first.text
    # P14: the claim stamps last_run_at with the due time, and moves next_run_at.
    assert first.json()["last_run_at"].startswith("2026-11-21T09:00:00")
    assert first.json()["next_run_at"].startswith("2026-11-22T09:00:00")
    again = _claim(env, row["id"], T0, nxt)
    assert again.status_code == 409 and again.json()["detail"] == "claim_lost"


def test_a_paused_routine_cannot_be_claimed(env):
    row = _mk(env)
    env.client.patch(f"/v1/users/{USER_A}/routines/{row['id']}", json={"state": "paused"}, headers=_h())
    r = _claim(env, row["id"], T0, T0 + timedelta(days=1))
    assert r.status_code == 409 and r.json()["detail"] == "claim_lost"


def test_three_failures_in_a_row_pause_the_routine(env):
    rid = _mk(env)["id"]
    seen = []
    for outcome in ("failed", "failed", "ok", "failed", "failed", "failed"):
        r = _outcome(env, rid, outcome, **({"error_code": "boom"} if outcome == "failed" else {}))
        assert r.status_code == 200, r.text
        seen.append((r.json()["state"], r.json()["consecutive_failures"]))
    assert seen == [
        ("active", 1), ("active", 2), ("active", 0), ("active", 1), ("active", 2), ("paused", 3),
    ]
    out = env.client.get(f"/v1/users/{USER_A}/routines/{rid}", headers=_h()).json()
    assert (out["last_outcome"], out["last_error_code"]) == ("failed", "boom")
    assert _outcome(env, "00000000-0000-0000-0000-000000000000", "ok").status_code == 404


def test_moving_to_active_resets_the_failures_and_next_run_can_be_set(env):
    rid = _mk(env)["id"]
    for _ in range(3):
        _outcome(env, rid, "failed")
    url = f"/v1/users/{USER_A}/routines/{rid}"
    paused = env.client.get(url, headers=_h()).json()
    assert paused["state"] == "paused" and paused["consecutive_failures"] == 3
    nxt = T0 + timedelta(days=3)
    r = env.client.patch(url, json={"state": "active", "next_run_at": _iso(nxt)}, headers=_h())
    assert r.status_code == 200
    assert (r.json()["state"], r.json()["consecutive_failures"]) == ("active", 0)
    assert r.json()["next_run_at"].startswith("2026-11-24T09:00:00")
    # Pausing keeps the count; the thread link is not patchable.
    assert env.client.patch(url, json={"state": "paused"}, headers=_h()).json()["consecutive_failures"] == 0
    assert env.client.patch(url, json={"thread_id": "x"}, headers=_h()).status_code == 422


def test_routine_spend_adds_per_local_day(env):
    url = f"/v1/users/{USER_A}/routine-spend"
    assert env.client.get(url, params={"day": "2026-11-21"}, headers=_h()).json() == {"spent_usd": 0}
    assert env.client.post(url, json={"day": "2026-11-21", "add_usd": 0.25}, headers=_h()).json() == {"spent_usd": 0.25}
    assert env.client.post(url, json={"day": "2026-11-21", "add_usd": 0.5}, headers=_h()).json() == {"spent_usd": 0.75}
    assert env.client.post(url, json={"day": "2026-11-22", "add_usd": 1}, headers=_h()).json() == {"spent_usd": 1}
    assert env.client.get(url, params={"day": "2026-11-21"}, headers=_h()).json() == {"spent_usd": 0.75}
    assert env.client.get(f"/v1/users/{USER_B}/routine-spend", params={"day": "2026-11-21"}, headers=_h(USER_B)).json() == {"spent_usd": 0}
    # A negative add is the reconcile; the total floors at 0.
    assert env.client.post(url, json={"day": "2026-11-21", "add_usd": -0.5}, headers=_h()).json() == {"spent_usd": 0.25}
    assert env.client.post(url, json={"day": "2026-11-21", "add_usd": -9}, headers=_h()).json() == {"spent_usd": 0}


def test_two_reserves_of_one_dollar_against_a_one_dollar_budget_grant_once(env):
    url = f"/v1/users/{USER_A}/routine-spend/reserve"
    body = {"day": "2026-11-21", "want_usd": 1.0, "budget_usd": 1.0}
    assert env.client.post(url, json=body, headers=_h()).json() == {"granted_usd": 1.0}
    assert env.client.post(url, json=body, headers=_h()).json() == {"granted_usd": 0}
    got = env.client.get(f"/v1/users/{USER_A}/routine-spend", params={"day": "2026-11-21"}, headers=_h())
    assert got.json() == {"spent_usd": 1.0}
    # A partial grant takes what is left; a reconcile frees budget again.
    env.client.post(f"/v1/users/{USER_A}/routine-spend", json={"day": "2026-11-21", "add_usd": -0.6}, headers=_h())
    part = env.client.post(url, json={**body, "want_usd": 5.0}, headers=_h())
    assert part.json() == {"granted_usd": 0.6}


def test_deleting_the_user_deletes_their_routines(env):
    _mk(env)
    _mk(env, USER_B)
    env.client.post(f"/v1/users/{USER_A}/routine-spend", json={"day": "2026-11-21", "add_usd": 1}, headers=_h())

    async def drop(s):
        await s.execute(delete(User).where(User.id == USER_A))
        await s.commit()

    env.db(drop)  # the fixture's connections run with PRAGMA foreign_keys=ON
    assert env.count(Routine) == 1 and env.count(Routine, Routine.user_id == USER_B) == 1
    assert env.count(RoutineSpendDay) == 0


def test_an_unread_only_patch_keeps_updated_at(env):
    thread = env.thread()
    assert thread["unread"] is False and thread["routine_id"] is None
    url = f"/v1/chat/threads/{thread['id']}"
    r = env.client.patch(url, json={"unread": True}, headers=_h())
    assert r.status_code == 200 and r.json()["unread"] is True
    assert r.json()["updated_at"] == thread["updated_at"]
    both = env.client.patch(url, json={"unread": False, "title": "Hi"}, headers=_h()).json()
    assert both["unread"] is False and both["title"] == "Hi"
    assert both["updated_at"] != thread["updated_at"]


def test_the_thread_list_carries_unread_and_routine_id(env):
    thread = env.thread()
    row = _mk(env, thread_id=thread["id"])
    env.client.patch(f"/v1/chat/threads/{thread['id']}", json={"unread": True}, headers=_h())
    (item,) = env.client.get("/v1/chat/threads", headers=_h()).json()
    assert item["unread"] is True and item["routine_id"] == row["id"]
    assert env.client.get(f"/v1/chat/threads/{thread['id']}", headers=_h()).json()["routine_id"] == row["id"]


def test_creating_with_another_users_thread_is_refused(env):
    other = env.thread(USER_B)
    r = env.client.post(f"/v1/users/{USER_A}/routines", json={**BODY, "thread_id": other["id"]}, headers=_h())
    assert r.status_code == 404 and env.count(Routine) == 0
    assert env.client.get(f"/v1/chat/threads/{other['id']}", headers=_h(USER_B)).json()["routine_id"] is None


def test_deleting_a_routine_keeps_its_thread_and_clears_the_link(env):
    thread = env.thread()
    row = _mk(env, thread_id=thread["id"])
    assert env.client.delete(f"/v1/users/{USER_A}/routines/{row['id']}", headers=_h()).status_code == 204
    kept = env.client.get(f"/v1/chat/threads/{thread['id']}", headers=_h())
    assert kept.status_code == 200 and kept.json()["routine_id"] is None
    assert env.client.get(f"/v1/users/{USER_A}/routines", headers=_h()).json() == []


@pytest.mark.parametrize("action", ["routine.save", "routine.delete"])
def test_a_routine_approval_row_is_accepted(env, action):
    subject = "routine:abc|v1"
    thread_id, approval_id, token = env.approved(action=action, subject=subject, amount_usd=0)
    stored = env.client.get(f"/v1/chat/approvals/{approval_id}", headers=_h()).json()
    assert (stored["action"], stored["status"]) == (action, "approved")
    r = env.consume(_consume_body(thread_id, token, action=action, subject=subject, needed_usd=0))
    assert r.status_code == 200 and r.json()["status"] == "used"


def _migration():
    spec = importlib.util.spec_from_file_location("migration_0050_routines", MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_0050_renders_the_tables_columns_and_check():
    module = _migration()
    assert module.revision == "0050" and module.down_revision == "0049"
    sql = _render(module.upgrade)
    assert "CREATE TABLE routines" in sql and "CREATE TABLE routine_spend_days" in sql
    assert sql.count("FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE") == 2
    assert "PRIMARY KEY (user_id, day)" in sql
    assert "CREATE INDEX ix_routines_state_next_run ON routines (state, next_run_at)" in sql
    assert "ALTER TABLE chat_threads ADD COLUMN unread BOOLEAN DEFAULT false NOT NULL" in sql
    assert "ALTER TABLE chat_threads ADD COLUMN routine_id TEXT" in sql
    assert "'playbook.save', 'routine.save', 'routine.delete')" in sql
    down = _render(module.downgrade)
    assert down.count("RAISE EXCEPTION") == 1 and "DROP TABLE routines" in down


def test_the_models_match_the_migration_check_set():
    assert {c.name for c in ChatThread.__table__.columns} >= {"unread", "routine_id"}
    check = next(c for c in __import__("app.models", fromlist=["ChatApproval"]).ChatApproval.__table__.constraints
                 if c.name == "ck_chat_approvals_action")
    assert "'clipstyle.save', 'clipstyle.publish', 'scene.build', 'scene.render')" in str(check.sqltext)
