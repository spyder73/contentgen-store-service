"""Routine budgets (migration 0057): a funded production with an end date and its ticks, and a press's choices."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.schemas import ProductionPatch
from tests.test_chat import USER_A, _h, _in, _sha
from tests.test_chat_productions import BODY as PRODUCTION
from tests.test_routines import _mk, env  # noqa: F401  (env is the fixture: it builds the routine tables too)


def _ends(**delta) -> str:
    return (datetime.now(timezone.utc) + timedelta(**delta)).isoformat()


def _budget(**over) -> dict:
    return {**PRODUCTION, "budget_usd": 3.0, "ends_at": _ends(days=7), "allowed": ["images", "video"], **over}


def _routine_thread(env) -> dict:
    thread = env.thread()
    _mk(env, thread_id=thread["id"])
    return thread


def _open(env, thread_id: str, **over):
    return env.client.post(f"/v1/chat/threads/{thread_id}/productions", json=_budget(**over), headers=_h())


def _patch(env, production_id: str, **body):
    return env.client.patch(f"/v1/chat/productions/{production_id}", json=body, headers=_h())


def _funded(env) -> dict:
    r = _open(env, _routine_thread(env)["id"])
    assert r.status_code == 201, r.text
    return r.json()


def test_a_routine_budget_opens_funded_with_its_end_and_ticks(env):
    thread = _routine_thread(env)
    ends = _ends(days=7)
    r = _open(env, thread["id"], ends_at=ends)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["budget_usd"] == 3.0 and body["budget_version"] == 1
    assert body["allowed"] == ["images", "video"]
    assert datetime.fromisoformat(body["ends_at"]).replace(tzinfo=timezone.utc) == datetime.fromisoformat(ends)


def test_a_budget_on_an_ordinary_thread_is_refused(env):
    r = _open(env, env.thread()["id"])
    assert r.status_code == 422 and r.json()["detail"] == "routine_budget_invalid"


@pytest.mark.parametrize("keep", [("budget_usd",), ("ends_at",), ("allowed",), ("budget_usd", "ends_at"), ("ends_at", "allowed")])
def test_budget_end_and_ticks_go_together(env, keep):
    body = {k: v for k, v in _budget().items() if k in PRODUCTION or k in keep}
    r = env.client.post(f"/v1/chat/threads/{_routine_thread(env)['id']}/productions", json=body, headers=_h())
    assert r.status_code == 422


def test_budget_bounds(env):
    thread = _routine_thread(env)

    def code(**over) -> int:
        r = _open(env, thread["id"], **over)
        if r.status_code == 201:  # one open production per thread
            _patch(env, r.json()["id"], close={"outcome": "x"})
        return r.status_code

    assert code(budget_usd=0) == 422 and code(budget_usd=10.01) == 422
    assert code(ends_at=_ends(minutes=-1)) == 422
    assert code(ends_at=_ends(days=14, hours=1, minutes=5)) == 422
    assert code(budget_usd=10.0, ends_at=_ends(days=14)) == 201
    assert code(allowed=[]) == 201
    assert code(allowed=["Images"]) == 422 and code(allowed=["a"] * 11) == 422
    assert _open(env, thread["id"], allowed=["a", "b", "a"]).json()["allowed"] == ["a", "b"]


def test_a_routine_budget_is_never_raised_or_reticked(env):
    pid = _funded(env)["id"]
    r = _patch(env, pid, add_budget_usd=1, expect_budget_version=1)
    assert r.status_code == 409 and r.json()["detail"] == "routine_budget_fixed"
    assert "allowed" not in ProductionPatch.model_fields
    assert _patch(env, pid, add_agent_spent_usd=0.5).json()["agent_spent_usd"] == 0.5
    assert _patch(env, pid, append_log=[{"text": "hi"}]).status_code == 200
    assert _patch(env, pid, watches=[{"kind": "render", "status": "rendering", "clip_id": "c"}]).status_code == 200
    closed = _patch(env, pid, close={"outcome": "done"}).json()
    assert closed["status"] == "closed"
    assert closed["budget_usd"] == 3.0 and closed["allowed"] == ["images", "video"] and closed["ends_at"]


def test_a_build_watch_round_trips(env):
    pid = _funded(env)["id"]
    watch = {"kind": "build", "status": "running", "session_id": "s-1", "cap_usd": 1.5}
    got = _patch(env, pid, watches=[watch]).json()["watches"][0]
    assert got["session_id"] == "s-1" and got["cap_usd"] == 1.5
    old = _patch(env, pid, watches=[{"kind": "render", "status": "rendering"}]).json()["watches"][0]
    assert old["session_id"] == "" and old["cap_usd"] == 0


def test_two_budgets_never_open_on_one_thread(env):
    thread = _routine_thread(env)
    assert _open(env, thread["id"]).status_code == 201
    r = _open(env, thread["id"])
    assert r.status_code == 409 and r.json()["detail"] == "production_open"


def _press(env, thread_id: str, token: str, **body):
    approval = env.approval(thread_id, action="production.budget")
    r = env.client.post(
        f"/v1/chat/approvals/{approval['id']}/approve",
        json={"token_sha256": _sha(token),
              "expires_at": _in(10), **body},
        headers=_h(),
    )
    return approval, r


def _consume(env, thread_id: str, token: str):
    return env.consume({
        "thread_id": thread_id, "token_sha256": _sha(token), "action": "production.budget",
        "subject": "series-1", "needed_usd": 1.0,
    })


def test_a_press_keeps_its_choices_and_consume_returns_them(env):
    thread = env.thread()
    _, r = _press(env, thread["id"], "t1", choices=["images", "video", "images"])
    assert r.status_code == 200 and r.json()["choices"] == ["images", "video"]
    assert _consume(env, thread["id"], "t1").json()["choices"] == ["images", "video"]


def test_a_press_with_no_ticks_stores_an_empty_list(env):
    thread = env.thread()
    _, r = _press(env, thread["id"], "t1", choices=[])
    assert r.json()["choices"] == []
    assert _consume(env, thread["id"], "t1").json()["choices"] == []


def test_a_press_without_choices_stores_none(env):
    thread = env.thread()
    _, r = _press(env, thread["id"], "t1")
    assert r.json()["choices"] is None
    assert _consume(env, thread["id"], "t1").json()["choices"] is None


def test_a_bad_choice_key_is_refused(env):
    thread = env.thread()
    assert _press(env, thread["id"], "t1", choices=["Bad Key"])[1].status_code == 422
    assert _press(env, thread["id"], "t2", choices=["a"] * 11)[1].status_code == 422


def test_deleting_the_routine_closes_its_budget_only(env):
    thread = env.thread()
    routine = _mk(env, thread_id=thread["id"])
    budget = _open(env, thread["id"]).json()
    other = env.thread()
    ordinary = env.client.post(f"/v1/chat/threads/{other['id']}/productions", json=PRODUCTION, headers=_h()).json()
    assert env.client.delete(f"/v1/users/{USER_A}/routines/{routine['id']}", headers=_h()).status_code == 204
    got = env.client.get(f"/v1/chat/productions/{budget['id']}", headers=_h()).json()
    assert got["status"] == "closed" and got["outcome"] == "routine_deleted" and got["closed_at"]
    assert env.client.get(f"/v1/chat/productions/{ordinary['id']}", headers=_h()).json()["status"] == "open"


def test_deleting_a_routine_with_an_ordinary_open_production_leaves_it(env):
    thread = env.thread()
    routine = _mk(env, thread_id=thread["id"])
    plain = env.client.post(f"/v1/chat/threads/{thread['id']}/productions", json=PRODUCTION, headers=_h()).json()
    env.client.delete(f"/v1/users/{USER_A}/routines/{routine['id']}", headers=_h())
    assert env.client.get(f"/v1/chat/productions/{plain['id']}", headers=_h()).json()["status"] == "open"


def test_an_ordinary_production_is_unchanged(env):
    thread = env.thread()
    body = env.client.post(f"/v1/chat/threads/{thread['id']}/productions", json=PRODUCTION, headers=_h()).json()
    assert body["ends_at"] is None and body["allowed"] is None
    assert body["budget_usd"] == 0 and body["budget_version"] == 0
