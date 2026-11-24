"""The chat list: what each thread waits on, made and spent, read in a fixed number of statements."""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db import get_session
from app.fastapi_app import create_fastapi_app
from app.models import ChatApproval, ChatThread, CreditsLedger, MediaItem, Routine, RoutineSpendDay
from app.stores import chat_list, credits_spent
from tests.test_chat import SECRET, USER_A, USER_B, Env, _create_tables, _h, _statements
from tests.test_chat_productions import _new, _patch
from tests.test_credits_hold_release import Ledger

AT = datetime(2026, 11, 24, 9, 0, tzinfo=timezone.utc)
HOLD_068 = 68  # credits of a $0.40 hold


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """The chat harness plus routines and the credits ledger."""
    monkeypatch.setenv("INTERNAL_API_SECRET", SECRET)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'list.db'}", poolclass=NullPool)
    engine.sync_engine.dialect.supports_native_uuid = True

    @event.listens_for(engine.sync_engine, "connect")
    def _register(dbapi_conn, _record):
        dbapi_conn.create_function("gen_random_uuid", 0, lambda: str(uuid.uuid4()))

    async def create() -> None:
        await _create_tables(engine)
        async with engine.begin() as conn:
            for table in (Routine.__table__, RoutineSpendDay.__table__, MediaItem.__table__, CreditsLedger.__table__):
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


def _list(env: Env, query: str = "") -> dict[str, dict]:
    r = env.client.get(f"/v1/chat/threads{query}", headers=_h())
    assert r.status_code == 200, r.text
    return {t["id"]: t for t in r.json()}


def _one(env: Env, thread_id: str) -> dict:
    return _list(env)[thread_id]


def _routine(env: Env, user: str = USER_A, **over) -> dict:
    body = {"name": "n", "prompt": "p", "cadence": "daily", "time_of_day": "07:00",
            "time_zone": "Europe/London", "next_run_at": AT.isoformat(), **over}
    r = env.client.post(f"/v1/users/{user}/routines", json=body, headers=_h(user))
    assert r.status_code == 200, r.text
    return r.json()


def _close(env: Env, production_id: str, outcome: str = "done"):
    assert _patch(env, production_id, close={"outcome": outcome}).status_code == 200


# ── the pressable-card rule ──────────────────────────────────────────────────

OPEN = str(uuid.uuid4())


@pytest.mark.parametrize(
    "action, subject, expected",
    [
        ("playbook.save", "series-1", True),
        ("routine.save", "", True),
        ("clipstyle.build", "style-1", True),
        ("scene.render", "video-1", True),
        ("run.start", "series-1", False),
        ("run.continue", "series-1", False),
        ("plan.approve", "series-1", False),
        ("graph.extend", "series-1", False),
        ("image.generate", "run:r1|x", False),
        ("frames.approve", "run:r1|frames:0", False),
        ("frames.approve", "series-1", False),
        ("frames.approve", f"production:{OPEN}|preview:p1", True),
        ("frames.approve", f"production:{OPEN}|preview:", False),
        ("frames.approve", f"production:{OPEN}|preview:a|b", False),
        ("production.budget", f"production:{OPEN}|v0", True),
        ("production.budget", f"production:{OPEN}x|v0", False),
        ("production.budget", f"production:{OPEN}", False),
    ],
)
def test_pressable(action, subject, expected):
    assert chat_list.pressable(action, subject, OPEN) is expected


def test_a_production_card_with_no_open_production_is_never_pressable():
    assert chat_list.pressable("production.budget", f"production:{OPEN}|v0", None) is False
    assert chat_list.pressable("playbook.save", "series-1", None) is True


# ── pending cards ────────────────────────────────────────────────────────────


def test_every_pressable_pending_card_needs_you(env):
    t = env.thread()
    env.approval(t["id"], action="playbook.save", subject="series-1", summary="Save the **cat** playbook", amount_usd=0)
    row = _one(env, t["id"])
    assert row["needs_you"] is True and row["pending_count"] == 1
    assert (row["pending_action"], row["pending_summary"]) == ("playbook.save", "Save the cat playbook")


def test_a_thread_with_nothing_pending_says_so(env):
    row = _one(env, env.thread()["id"])
    assert (row["needs_you"], row["pending_count"], row["pending_action"], row["pending_summary"]) == (
        False, 0, None, ""
    )
    assert row["pending_amount_usd"] == 0 and row["latest_production"] is None and row["routine_info"] is None


def test_retired_and_stale_cards_never_count(env):
    t = env.thread()
    for action in ("run.start", "run.continue", "plan.approve", "graph.extend"):
        env.approval(t["id"], action=action, subject="series-1")
    env.approval(t["id"], action="frames.approve", subject="run:r1|frames:0")
    closed = _new(env, t["id"])
    env.approval(t["id"], action="production.budget", subject=f"production:{closed['id']}|v0")
    _close(env, closed["id"])
    new = _new(env, t["id"])
    env.approval(t["id"], action="production.budget", subject=f"production:{new['id']}x|v0")
    env.approval(t["id"], action="production.budget", subject=f"production:{closed['id']}|v1")
    row = _one(env, t["id"])
    assert (row["needs_you"], row["pending_count"], row["pending_action"]) == (False, 0, None)


def _age(env: Env, approval_id: str, days: float) -> None:
    """Moves a card's creation ``days`` back from now."""

    async def run(session):
        at = datetime.now(timezone.utc) - timedelta(days=days)
        await session.execute(update(ChatApproval).where(ChatApproval.id == approval_id).values(created_at=at))
        await session.commit()

    env.db(run)


def test_the_age_limit_is_fourteen_days():
    assert chat_list.PENDING_CARD_MAX_AGE == timedelta(days=14)


def test_a_card_counts_for_at_most_fourteen_days(env):
    recent, old = env.thread(), env.thread()
    _age(env, env.approval(recent["id"], action="playbook.save", subject="series-1", summary="Recent")["id"], 13)
    _age(env, env.approval(old["id"], action="playbook.save", subject="series-1", summary="Old")["id"], 15)
    rows = _list(env)
    assert (rows[recent["id"]]["needs_you"], rows[recent["id"]]["pending_count"]) == (True, 1)
    assert (rows[old["id"]]["needs_you"], rows[old["id"]]["pending_count"], rows[old["id"]]["pending_action"]) == (
        False, 0, None
    )


def test_an_old_card_beside_a_recent_one_is_not_counted(env):
    t = env.thread()
    _age(env, env.approval(t["id"], action="routine.save", subject="r", summary="Long gone")["id"], 15)
    _age(env, env.approval(t["id"], action="playbook.save", subject="series-1", summary="Still waiting")["id"], 2)
    row = _one(env, t["id"])
    assert (row["pending_count"], row["pending_action"], row["pending_summary"]) == (1, "playbook.save", "Still waiting")


def test_answered_cards_do_not_count(env):
    t = env.thread()
    a = env.approval(t["id"], action="playbook.save", subject="series-1")
    assert env.client.post(f"/v1/chat/approvals/{a['id']}/decline", headers=_h()).status_code == 200
    assert _one(env, t["id"])["pending_count"] == 0


def test_pending_facts_name_the_newest_card(env):
    t = env.thread()
    env.approval(t["id"], action="playbook.save", subject="series-1", summary="Older", amount_usd=1.0)
    env.approval(t["id"], action="routine.save", subject="r", summary="x " * 200, amount_usd=2.5)
    row = _one(env, t["id"])
    assert (row["pending_count"], row["pending_action"], row["pending_amount_usd"]) == (2, "routine.save", 2.5)
    assert len(row["pending_summary"]) <= 140 and row["pending_summary"].endswith("…")


# ── productions and spend ────────────────────────────────────────────────────


def test_latest_production_is_the_open_one_else_the_newest_closed(env):
    t = env.thread()
    first = _new(env, t["id"])
    _close(env, first["id"], "first outcome")
    second = _new(env, t["id"])
    _close(env, second["id"], "second outcome")
    row = _one(env, t["id"])["latest_production"]
    assert (row["id"], row["status"], row["outcome"]) == (second["id"], "closed", "second outcome")
    assert row["closed_at"] is not None and row["settled_usd"] is None and row["held_usd"] is None
    third = _new(env, t["id"])
    row = _one(env, t["id"])["latest_production"]
    assert (row["id"], row["status"], row["closed_at"]) == (third["id"], "open", None)


def test_the_list_keeps_the_jobs_running_flag(env):
    t = env.thread()
    p = _new(env, t["id"])
    assert _one(env, t["id"])["jobs_running"] is False
    watch = {"kind": "render", "clip_id": "c1", "status": "rendering"}
    assert _patch(env, p["id"], watches=[watch]).status_code == 200
    assert _one(env, t["id"])["jobs_running"] is True
    _close(env, p["id"])
    assert _one(env, t["id"])["jobs_running"] is False


def _budget(env: Env, production_id: str, usd: float) -> None:
    r = _patch(env, production_id, add_budget_usd=usd, expect_budget_version=0)
    assert r.status_code == 200, r.text


def test_open_production_spend_matches_the_ledger(env):
    ledger = Ledger(env.factory)
    t = env.thread()
    p = _new(env, t["id"])
    _budget(env, p["id"], 5.0)
    env.db(lambda s: _grant(s))

    async def go():
        await ledger.reserve(p["id"], "gen-a", 120, user=USER_A)
        await ledger.settle(p["id"], "gen-a", "1.5", user=USER_A)
        await ledger.reserve(p["id"], "gen-b", HOLD_068, user=USER_A)

    asyncio.run(go())
    shown = _one(env, t["id"])["latest_production"]

    async def truth(s):
        return (await credits_spent.spent_by_scope(s, USER_A, {"x": [p["id"]]}))["x"]

    expected = env.db(truth)
    assert expected["usd"] == pytest.approx(1.5) and expected["held_usd"] == pytest.approx(0.40)
    assert (shown["settled_usd"], shown["held_usd"], shown["budget_usd"]) == (
        expected["usd"], expected["held_usd"], 5.0
    )
    _close(env, p["id"])
    closed = _one(env, t["id"])["latest_production"]
    assert (closed["settled_usd"], closed["held_usd"]) == (None, None)


async def _grant(s) -> None:
    from sqlalchemy import update
    from app.models import User

    await s.execute(update(User).where(User.id == USER_A).values(credits_balance=1000))
    await s.commit()


def test_a_production_without_a_budget_runs_no_ledger_statement(env):
    t = env.thread()
    _new(env, t["id"])
    with _statements(env) as seen:
        row = _one(env, t["id"])["latest_production"]
    assert (row["status"], row["budget_usd"], row["settled_usd"], row["held_usd"]) == ("open", 0, None, None)
    assert not any("credits_ledger" in sql for sql in seen)


# ── routines ─────────────────────────────────────────────────────────────────


def _bind(env: Env, thread_id: str, routine_id: str) -> None:
    env.set_row(ChatThread, thread_id, routine_id=routine_id)


def test_routine_facts(env):
    r = _routine(env)
    t = env.thread()
    _bind(env, t["id"], r["id"])
    out = env.client.post(
        f"/v1/routines/{r['id']}/outcome",
        json={"outcome": "failed", "at": AT.isoformat(), "error_code": "boom"},
        headers=_h(),
    )
    assert out.status_code == 200
    env.db(lambda s: _run_count(s, r["id"], 12))
    info = _one(env, t["id"])["routine_info"]
    assert info == {
        "cadence": "daily", "weekday": None, "time_of_day": "07:00", "state": "active", "run_count": 12,
        "last_outcome": "failed", "last_error_code": "boom", "last_run_at": info["last_run_at"],
    }
    assert info["last_run_at"].startswith("2026-11-24T09:00:00")


async def _run_count(s, routine_id: str, count: int) -> None:
    from sqlalchemy import update

    await s.execute(update(Routine).where(Routine.id == routine_id).values(run_count=count))
    await s.commit()


def test_another_users_routine_is_not_shown(env):
    theirs = _routine(env, USER_B)
    t = env.thread()
    _bind(env, t["id"], theirs["id"])
    _bind(env, env.thread()["id"], "not-a-uuid")
    assert all(row["routine_info"] is None for row in _list(env).values())


# ── what the list costs ──────────────────────────────────────────────────────


def _busy_thread(env: Env) -> str:
    t = env.thread()
    p = _new(env, t["id"])
    _budget(env, p["id"], 2.0)
    env.approval(t["id"], action="playbook.save", subject="series-1")
    _bind(env, t["id"], _routine_for(env))
    return t["id"]


def _routine_for(env: Env) -> str:
    # The per-user routine limit is 5: one routine serves every busy thread.
    existing = env.db(lambda s: _first_routine(s))
    return existing or _routine(env)["id"]


async def _first_routine(s) -> str | None:
    return (await s.execute(select(Routine.id).limit(1))).scalar_one_or_none()


def _count_statements(env: Env) -> int:
    with _statements(env) as seen:
        assert len(_list(env)) > 0
    return len(seen)


def test_list_reads_a_fixed_number_of_statements(env):
    _busy_thread(env)
    one = _count_statements(env)
    for _ in range(29):
        _busy_thread(env)
    assert len(_list(env)) == 30
    many = _count_statements(env)
    # threads, productions, pending cards, routines, then the ledger's held and debit reads.
    assert (one, many) == (6, 6)


def test_without_a_budgeted_open_production_the_list_reads_four_statements(env):
    for _ in range(3):
        t = env.thread()
        _new(env, t["id"])
        env.approval(t["id"], action="playbook.save", subject="series-1")
        _bind(env, t["id"], _routine_for(env))
    assert _count_statements(env) == 4


def test_list_never_selects_the_heavy_columns(env):
    _busy_thread(env)
    with _statements(env) as seen:
        assert len(_list(env)) == 1
    assert seen, "no statement was captured"
    joined = " ".join(seen)
    for heavy in ("transcript", "known_media", "chat_productions.log", "artifacts", "breakdown"):
        assert heavy not in joined, heavy
    assert "chat_productions.watches" in joined  # the one JSON column it needs, for jobs_running
