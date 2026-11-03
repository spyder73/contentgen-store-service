"""Chat productions: a durable work item per piece of work (migration 0045).

A production belongs to a chat thread. It carries the approved generation budget, the
assistant's own cost cap, a log, the artifacts and the render watches a wake-up resumes
from. At most one production per thread is open (a partial unique index). The same
migration lets an approval be ``production.budget``, and ``/spent`` reports ``held_usd``,
the open holds of a scope, which a production's admission check adds to what it spent.

The behaviour tests use the real-app harness of ``test_chat.py`` (a sqlite database built
from the models). The migration's DDL is asserted by rendering it offline in the postgres
dialect. The ledger tests use the real ORM ledger of ``test_credits_hold_release.py``.
"""
from __future__ import annotations

import importlib.util
import re
from datetime import datetime
from pathlib import Path

import pytest

from app.fastapi_app import create_fastapi_app
from app.db import get_session
from app.models import ChatApproval, ChatProduction
from app.stores import credits, credits_spent, productions
from tests.test_chat import USER_A, USER_B, Env, _consume_body, _h, _render, env  # noqa: F401  (env is the fixture)
from tests.test_credits_hold_release import OTHER, SECRET, USER, _run, httpx, ledger  # noqa: F401

MIGRATION_PATH = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0045_chat_productions.py"

ACTIONS_0044 = (
    "'run.start', 'run.continue', 'image.generate', 'clip.render', 'plan.approve', 'frames.approve', "
    "'graph.extend', 'sourcing.run'"
)
ACTIONS_0045 = f"{ACTIONS_0044}, 'production.budget'"

BODY = {"title": "Cat week", "goal": "Ten pictures of cats", "agent_cap_usd": 5.0}


def _new(env: Env, thread_id: str | None = None, user: str = USER_A, **over) -> dict:
    """Open a production on a new thread (or the given one); returns its JSON."""
    thread_id = thread_id or env.thread(user)["id"]
    r = env.client.post(f"/v1/chat/threads/{thread_id}/productions", json={**BODY, **over}, headers=_h(user))
    assert r.status_code == 201, r.text
    return r.json()


def _patch(env: Env, production_id: str, user: str = USER_A, **body):
    return env.client.patch(f"/v1/chat/productions/{production_id}", json=body, headers=_h(user))


def _get(env: Env, production_id: str, user: str = USER_A) -> dict:
    r = env.client.get(f"/v1/chat/productions/{production_id}", headers=_h(user))
    assert r.status_code == 200, r.text
    return r.json()


# ── create and read ──────────────────────────────────────────────────────────


def test_a_new_production_starts_open_with_nothing_approved_or_spent(env):
    p = _new(env, playbook_slug="cat-pictures")
    assert (p["title"], p["goal"], p["playbook_slug"], p["status"]) == ("Cat week", "Ten pictures of cats", "cat-pictures", "open")
    assert (p["budget_usd"], p["budget_version"], p["agent_cap_usd"], p["agent_spent_usd"]) == (0, 0, 5.0, 0)
    assert (p["log"], p["artifacts"], p["watches"]) == ([], {"media_ids": [], "clip_ids": []}, [])
    assert (p["wake_seq"], p["acked_seq"], p["wake_reason"], p["outcome"], p["closed_at"]) == (0, 0, "", "", None)
    assert p["user_id"] == USER_A and p["created_at"] and p["updated_at"]
    assert _get(env, p["id"]) == p
    assert _new(env)["playbook_slug"] is None


def test_a_thread_has_at_most_one_open_production(env):
    first = _new(env)
    thread_id = first["thread_id"]
    again = env.client.post(f"/v1/chat/threads/{thread_id}/productions", json=BODY, headers=_h())
    assert (again.status_code, again.json()) == (409, {"detail": "production_open"})
    assert env.count(ChatProduction, ChatProduction.thread_id == thread_id) == 1
    # Another thread is not blocked, and the open one is found by thread.
    _new(env)
    open_one = env.client.get(f"/v1/chat/threads/{thread_id}/productions/open", headers=_h())
    assert open_one.json()["id"] == first["id"]

    assert _patch(env, first["id"], close={"outcome": "shipped"}).status_code == 200
    assert env.client.get(f"/v1/chat/threads/{thread_id}/productions/open", headers=_h()).status_code == 404
    second = _new(env, thread_id)
    assert second["id"] != first["id"]
    assert env.client.get(f"/v1/chat/threads/{thread_id}/productions/open", headers=_h()).json()["id"] == second["id"]


def test_productions_are_scoped_to_their_user(env):
    p = _new(env)
    url = f"/v1/chat/productions/{p['id']}"
    assert env.client.get(url, headers=_h(USER_B)).status_code == 404
    assert _patch(env, p["id"], USER_B, add_agent_spent_usd=1).status_code == 404
    assert env.client.get(f"/v1/chat/threads/{p['thread_id']}/productions/open", headers=_h(USER_B)).status_code == 404
    taken = env.client.post(f"/v1/chat/threads/{p['thread_id']}/productions", json=BODY, headers=_h(USER_B))
    assert (taken.status_code, taken.json()["detail"]) == (404, "thread_not_found")
    assert _get(env, p["id"])["agent_spent_usd"] == 0


def test_an_id_that_is_not_a_uuid_is_a_404(env):
    assert env.client.get("/v1/chat/productions/not-a-uuid", headers=_h()).status_code == 404
    assert _patch(env, "not-a-uuid", add_agent_spent_usd=1).status_code == 404
    assert env.client.get("/v1/chat/threads/not-a-uuid/productions/open", headers=_h()).status_code == 404


def test_a_production_needs_a_title_a_goal_and_a_non_negative_cap(env):
    thread_id = env.thread()["id"]
    url = f"/v1/chat/threads/{thread_id}/productions"
    for bad in ({"title": None}, {"goal": None}, {"agent_cap_usd": -1}, {"agent_cap_usd": None}):
        assert env.client.post(url, json={**BODY, **bad}, headers=_h()).status_code == 422, bad
    assert env.count(ChatProduction) == 0


# ── patch: money ─────────────────────────────────────────────────────────────


def test_budget_adds_only_on_the_current_version(env):
    p = _new(env)
    r = _patch(env, p["id"], expect_budget_version=0, add_budget_usd=1.5)
    assert r.status_code == 200
    assert (r.json()["budget_usd"], r.json()["budget_version"]) == (1.5, 1)

    stale = _patch(env, p["id"], expect_budget_version=0, add_budget_usd=1.5)
    assert (stale.status_code, stale.json()) == (409, {"detail": "stale_budget_version"})
    assert (_get(env, p["id"])["budget_usd"], _get(env, p["id"])["budget_version"]) == (1.5, 1)

    raised = _patch(env, p["id"], expect_budget_version=1, add_budget_usd=0.5).json()
    assert (raised["budget_usd"], raised["budget_version"]) == (2.0, 2)


def test_a_stale_budget_change_applies_nothing_else_of_the_patch(env):
    p = _new(env)
    _patch(env, p["id"], expect_budget_version=0, add_budget_usd=1)
    r = _patch(env, p["id"], expect_budget_version=0, add_budget_usd=1, add_agent_spent_usd=0.3,
               append_log=[{"text": "x"}], add_media_ids=["m1"])
    assert r.status_code == 409
    after = _get(env, p["id"])
    assert (after["agent_spent_usd"], after["log"], after["artifacts"]["media_ids"]) == (0, [], [])


def test_a_budget_change_names_its_version_and_amounts_are_not_negative(env):
    p = _new(env)
    for bad in (
        {"add_budget_usd": 1.0},
        {"expect_budget_version": 0},
        {"expect_budget_version": 0, "add_budget_usd": -1},
        {"add_agent_cap_usd": -1},
        {"add_agent_spent_usd": -0.01},
        {"acked_seq": -1},
    ):
        assert _patch(env, p["id"], **bad).status_code == 422, bad
    assert _get(env, p["id"]) == p


def test_the_update_refuses_by_itself_when_the_early_checks_are_skipped(env, monkeypatch):
    """The budget version and the closed status are guarded in the UPDATE's own WHERE clause."""
    monkeypatch.setattr(productions, "_refusal", lambda row, patch: None)
    p = _new(env)
    _patch(env, p["id"], expect_budget_version=0, add_budget_usd=1)
    stale = _patch(env, p["id"], expect_budget_version=0, add_budget_usd=1)
    assert (stale.status_code, stale.json()) == (409, {"detail": "stale_budget_version"})
    assert _get(env, p["id"])["budget_usd"] == 1

    _patch(env, p["id"], close={"outcome": "done"})
    late = _patch(env, p["id"], add_agent_spent_usd=0.5)
    assert (late.status_code, late.json()) == (409, {"detail": "production_closed"})
    assert _get(env, p["id"])["agent_spent_usd"] == 0


def test_agent_spent_and_cap_add_atomically(env):
    p = _new(env)
    _patch(env, p["id"], add_agent_spent_usd=0.25, add_agent_cap_usd=1)
    out = _patch(env, p["id"], add_agent_spent_usd=0.25, add_agent_cap_usd=1).json()
    assert (out["agent_spent_usd"], out["agent_cap_usd"]) == (0.5, 7.0)
    assert (out["budget_usd"], out["budget_version"]) == (0, 0)


# ── patch: log, artifacts, watches ───────────────────────────────────────────


def test_log_keeps_the_newest_200_and_stamps_at(env):
    p = _new(env)
    n = 0
    for _ in range(41):
        entries = []
        for _ in range(5):
            n += 1
            entries.append({"text": f"note {n}"})
        assert _patch(env, p["id"], append_log=entries).status_code == 200
    log = _get(env, p["id"])["log"]
    assert n == 205 and len(log) == 200
    assert (log[0]["text"], log[-1]["text"]) == ("note 6", "note 205")
    assert all(datetime.fromisoformat(e["at"]) for e in log)
    assert (log[0]["media_ids"], log[0]["clip_ids"]) == ([], [])


def test_a_log_entry_keeps_the_media_and_clips_it_names(env):
    p = _new(env)
    out = _patch(env, p["id"], append_log=[{"text": "a cover", "media_ids": ["m1", "m2"], "clip_ids": ["c1"]}]).json()
    assert [(e["text"], e["media_ids"], e["clip_ids"]) for e in out["log"]] == [("a cover", ["m1", "m2"], ["c1"])]
    # An empty list appends nothing.
    assert len(_patch(env, p["id"], append_log=[]).json()["log"]) == 1


def test_artifacts_dedupe(env):
    p = _new(env)
    _patch(env, p["id"], add_media_ids=["m1", "m2"], add_clip_ids=["c1"])
    out = _patch(env, p["id"], add_media_ids=["m1", "m3", "m3"], add_clip_ids=["c1"]).json()
    assert out["artifacts"] == {"media_ids": ["m1", "m2", "m3"], "clip_ids": ["c1"]}


def test_watches_replace_the_list(env):
    p = _new(env)
    one = {"kind": "render", "clip_id": "c1", "status": "rendering", "started_at": None}
    two = {"kind": "render", "clip_id": "c2", "status": "rendering", "started_at": None}
    assert _patch(env, p["id"], watches=[one, two]).json()["watches"] == [one, two]
    assert _patch(env, p["id"], watches=[two]).json()["watches"] == [two]
    assert _patch(env, p["id"], add_agent_spent_usd=0.1).json()["watches"] == [two]
    assert _patch(env, p["id"], watches=[]).json()["watches"] == []


# ── patch: wake-ups ──────────────────────────────────────────────────────────


def test_ack_never_passes_wake_seq(env):
    p = _new(env)
    woken = _patch(env, p["id"], bump_wake={"reason": "render_ready"}).json()
    assert (woken["wake_seq"], woken["acked_seq"], woken["wake_reason"]) == (1, 0, "render_ready")
    assert [x["id"] for x in env.client.get("/v1/chat/productions/pending-wakes", headers=_h()).json()] == [p["id"]]

    beyond = _patch(env, p["id"], acked_seq=2)
    assert (beyond.status_code, beyond.json()) == (422, {"detail": "ack_beyond_wake"})
    assert _get(env, p["id"])["acked_seq"] == 0

    acked = _patch(env, p["id"], acked_seq=1)
    assert acked.status_code == 200 and acked.json()["acked_seq"] == 1
    assert env.client.get("/v1/chat/productions/pending-wakes", headers=_h()).json() == []
    # An ack never moves back, and a second bump is a new wake-up.
    assert _patch(env, p["id"], acked_seq=0).json()["acked_seq"] == 1
    assert _patch(env, p["id"], bump_wake={"reason": "render_failed"}).json()["wake_seq"] == 2


def test_pending_wakes_lists_open_unacked_productions_of_every_user(env):
    waiting = _new(env)
    _patch(env, waiting["id"], bump_wake={"reason": "r"})
    theirs = _new(env, user=USER_B)
    _patch(env, theirs["id"], USER_B, bump_wake={"reason": "r"})
    quiet = _new(env)
    acked = _new(env)
    _patch(env, acked["id"], bump_wake={"reason": "r"}, acked_seq=1)
    closed = _new(env)
    _patch(env, closed["id"], bump_wake={"reason": "r"}, close={"outcome": "done"})
    # The sweep runs without a user: the internal secret is the gate.
    listed = env.client.get("/v1/chat/productions/pending-wakes", headers=_h(None)).json()
    assert {x["id"] for x in listed} == {waiting["id"], theirs["id"]}
    assert quiet["id"] not in {x["id"] for x in listed}


def test_pending_wakes_also_lists_open_productions_with_a_render_still_rendering(env):
    """A render a restart cut off owes no wake-up yet: the boot has to find it to resolve it."""

    def watch(clip: str, status: str = "rendering") -> dict:
        return {"kind": "render", "clip_id": clip, "status": status}

    rendering = _new(env)
    _patch(env, rendering["id"], watches=[watch("c0", "ready"), watch("c1")])
    theirs = _new(env, user=USER_B)
    _patch(env, theirs["id"], USER_B, watches=[watch("c2")])
    handled = _new(env)
    _patch(env, handled["id"], watches=[watch("c3")], bump_wake={"reason": "r"}, acked_seq=1)
    ended = _new(env)
    _patch(env, ended["id"], watches=[watch("c4", "ready"), watch("c5", "failed")])
    closed = _new(env)
    _patch(env, closed["id"], watches=[watch("c6")], close={"outcome": "dropped"})
    _new(env)  # waits on nothing

    listed = env.client.get("/v1/chat/productions/pending-wakes", headers=_h(None)).json()
    assert {x["id"] for x in listed} == {rendering["id"], theirs["id"], handled["id"]}


def test_a_watch_keeps_when_its_render_started(env):
    """The boot compares this with the clip's own finished_at. Rows from before it have none."""
    p = _new(env)
    started = {"kind": "render", "clip_id": "c1", "status": "rendering", "started_at": "2026-11-03T10:00:00.123456Z"}
    old = {"kind": "render", "clip_id": "c2", "status": "rendering"}
    assert _patch(env, p["id"], watches=[started, old]).status_code == 200

    watches = _get(env, p["id"])["watches"]
    assert watches[0] == started
    assert watches[1] == {**old, "started_at": None}

    # It is a timestamp, and the sweeps match a watch that has one.
    assert _patch(env, p["id"], watches=[{**old, "started_at": "soon"}]).status_code == 422
    found = env.client.get("/v1/chat/productions/watching?clip_id=c1", headers=_h(None)).json()
    assert [x["id"] for x in found] == [p["id"]]
    assert [x["id"] for x in env.client.get("/v1/chat/productions/pending-wakes", headers=_h(None)).json()] == [p["id"]]


def test_watching_finds_open_rendering_watches_only(env):
    def watch(clip: str, status: str = "rendering", kind: str = "render") -> dict:
        return {"kind": kind, "clip_id": clip, "status": status}

    a = _new(env)
    _patch(env, a["id"], watches=[watch("c0"), watch("cx")])
    b = _new(env)
    _patch(env, b["id"], watches=[watch("cx", "ready")])
    c = _new(env)
    _patch(env, c["id"], watches=[watch("cy")])
    d = _new(env)
    _patch(env, d["id"], watches=[watch("cx")], close={"outcome": "dropped"})
    e = _new(env)
    _patch(env, e["id"], watches=[watch("cx", kind="other")])
    f = _new(env, user=USER_B)
    _patch(env, f["id"], USER_B, watches=[watch("cx")])

    found = env.client.get("/v1/chat/productions/watching?clip_id=cx", headers=_h(None)).json()
    assert {x["id"] for x in found} == {a["id"], f["id"]}
    assert env.client.get("/v1/chat/productions/watching?clip_id=nope", headers=_h(None)).json() == []
    assert env.client.get("/v1/chat/productions/watching", headers=_h(None)).status_code == 422


# ── patch: closing ───────────────────────────────────────────────────────────


def test_close_records_the_outcome(env):
    p = _new(env)
    out = _patch(env, p["id"], close={"outcome": "ten pictures made"}).json()
    assert (out["status"], out["outcome"]) == ("closed", "ten pictures made")
    assert datetime.fromisoformat(out["closed_at"])


def test_closed_production_refuses_changes_but_takes_acks(env):
    p = _new(env)
    _patch(env, p["id"], bump_wake={"reason": "r"}, close={"outcome": "done"})
    for change in (
        {"add_agent_spent_usd": 0.1},
        {"expect_budget_version": 0, "add_budget_usd": 1},
        {"append_log": [{"text": "late"}]},
        {"add_media_ids": ["m1"]},
        {"watches": []},
        {"bump_wake": {"reason": "again"}},
        {"close": {"outcome": "again"}},
        {"acked_seq": 1, "add_media_ids": ["m1"]},  # an ack does not carry another change in
    ):
        r = _patch(env, p["id"], **change)
        assert (r.status_code, r.json()) == (409, {"detail": "production_closed"}), change
    after = _get(env, p["id"])
    assert (after["acked_seq"], after["outcome"], after["artifacts"]["media_ids"], after["log"]) == (0, "done", [], [])

    acked = _patch(env, p["id"], acked_seq=1)
    assert acked.status_code == 200 and acked.json()["acked_seq"] == 1
    assert _patch(env, p["id"], acked_seq=5).status_code == 422


def test_closed_productions_list_newest_first_for_their_user(env):
    old, new, still_open = _new(env), _new(env), _new(env)
    _patch(env, old["id"], close={"outcome": "old one"})
    _patch(env, new["id"], close={"outcome": "new one"})
    _new(env, user=USER_B)
    url = f"/v1/users/{USER_A}/chat-productions"
    listed = env.client.get(f"{url}?status=closed&limit=10", headers=_h()).json()
    assert [x["id"] for x in listed] == [new["id"], old["id"]]
    assert [x["id"] for x in env.client.get(f"{url}?limit=1", headers=_h()).json()] == [new["id"]]
    assert still_open["id"] not in {x["id"] for x in listed}
    assert env.client.get(f"{url}?status=open", headers=_h()).status_code == 422
    # The path user is the caller, as on the credits routes.
    assert env.client.get(url, headers=_h(USER_B)).status_code == 403
    assert env.client.get(f"/v1/users/{USER_B}/chat-productions", headers=_h(USER_B)).json() == []


def test_thread_delete_cascades(env):
    open_one, closed_one = _new(env), _new(env)
    _patch(env, closed_one["id"], close={"outcome": "done"})
    assert env.client.delete(f"/v1/chat/threads/{open_one['thread_id']}", headers=_h()).status_code == 204
    assert env.client.get(f"/v1/chat/productions/{open_one['id']}", headers=_h()).status_code == 404
    assert env.count(ChatProduction, ChatProduction.id == open_one["id"]) == 0
    assert env.client.delete(f"/v1/chat/threads/{closed_one['thread_id']}", headers=_h()).status_code == 204
    assert env.count(ChatProduction) == 0


# ── the production.budget approval ───────────────────────────────────────────

SUBJECT = "production:0b0f9f6e-6a3e-4c7e-9a52-0c2f1f0d7a11|v0|plan:" + "ab" * 32


def test_production_budget_is_an_approval_action(env):
    thread_id, approval_id, token = env.approved(action="production.budget", subject=SUBJECT, amount_usd=3.6)
    stored = env.client.get(f"/v1/chat/approvals/{approval_id}", headers=_h()).json()
    assert (stored["action"], stored["status"], stored["subject"]) == ("production.budget", "approved", SUBJECT)
    r = env.consume(_consume_body(thread_id, token, action="production.budget", subject=SUBJECT, needed_usd=3.6))
    assert r.status_code == 200 and r.json()["status"] == "used"
    assert env.count(ChatApproval, ChatApproval.action == "production.budget") == 1
    bad = env.client.post(
        f"/v1/chat/threads/{thread_id}/approvals", json={"action": "production.raise", "subject": "s"}, headers=_h()
    )
    assert bad.status_code == 422


# ── held_usd in /spent ───────────────────────────────────────────────────────

HOLD_068 = 68  # credits for a $0.40 hold: ceil(0.40 * 100 * 1.7)


@pytest.mark.asyncio
async def test_spent_reports_open_holds(ledger):
    scope = _run()

    async def read(*keys: str, user: str = USER) -> dict:
        async with ledger.factory() as s:
            return (await credits_spent.spent_by_scope(s, user, {"x": list(keys or [scope])}))["x"]

    assert await read() == {"credits": 0, "usd": 0.0, "held_usd": 0.0, "debit_count": 0}

    await ledger.reserve(scope, "gen-a", HOLD_068)
    out = await read()
    assert out["held_usd"] == pytest.approx(0.40) and out["usd"] == 0

    await ledger.settle(scope, "gen-a", "0.38")
    out = await read()
    assert out["held_usd"] == 0 and out["usd"] == pytest.approx(0.38)

    await ledger.reserve(scope, "gen-b", HOLD_068)
    assert (await read())["held_usd"] == pytest.approx(0.40)
    await ledger.release(scope, "failed", key="rel-b", cp="gen-b", attempt=1)
    out = await read()
    assert out["held_usd"] == 0 and out["usd"] == pytest.approx(0.38)
    await ledger.assert_books()


@pytest.mark.asyncio
async def test_held_usd_follows_the_ledgers_own_open_hold_matching(ledger):
    mine, theirs, legacy = _run(), _run(), _run()
    await ledger.reserve(mine, "gen-a", HOLD_068)
    await ledger.settle(mine, "gen-a", "0.40")                  # closed by a debit
    await ledger.reserve(mine, "gen-a", 34, attempt=2)          # a retry of the same step: its own hold, open
    await ledger.reserve(mine, "gen-b", HOLD_068)               # open
    await ledger.reserve(mine, "gen-c", 17)
    await ledger.release(mine, "failed", key="rel-c", cp="gen-c", attempt=1)  # closed by a release
    await ledger.reserve(mine, "gen-a", 51, user=OTHER)         # the same key held by somebody else
    await ledger.reserve(theirs, "gen-a", 85)                   # a run that is not in the scope
    await ledger.reserve(legacy, "gen-a", 17)
    await ledger.legacy_release(legacy, 17, "run_failed")       # closed by a run-wide release row
    await ledger.reserve(legacy, "gen-b", 17)                   # opened after it: still open

    async with ledger.factory() as s:
        out = await credits_spent.spent_by_scope(s, USER, {"one": [mine], "two": [mine, legacy], "none": [_run()]})
        open_credits = sum(c for _, _, c in await credits._open_holds(s, USER, mine))
    assert open_credits == HOLD_068 + 34, "the ledger's own matching sees the two open holds"
    assert out["one"]["held_usd"] == pytest.approx(float(credits.credits_to_usd(open_credits)))
    assert out["two"]["held_usd"] == pytest.approx(float(credits.credits_to_usd(open_credits + 17)))
    assert out["none"]["held_usd"] == 0
    # The other user's holds are theirs alone.
    async with ledger.factory() as s:
        other = await credits_spent.spent_by_scope(s, OTHER, {"one": [mine]})
    assert other["one"]["held_usd"] == pytest.approx(float(credits.credits_to_usd(51)))


@pytest.mark.asyncio
async def test_a_settle_between_the_two_reads_is_never_counted_in_neither(ledger, monkeypatch):
    """/spent reads holds and debits in two statements. A settle that commits between them closes the
    hold and writes the debit; the answer may count that call twice, but never zero times."""
    scope = _run()
    await ledger.reserve(scope, "gen-a", HOLD_068)
    real_held, real_debited = credits_spent._held_credits_by_key, credits_spent._debited_by_key
    landed: list[bool] = []

    def settle_after_the_first_read(real):
        async def read(*args, **kwargs):
            out = await real(*args, **kwargs)
            if not landed:  # whichever read ran first: the settle lands right after it
                landed.append(True)
                await ledger.settle(scope, "gen-a", "0.38")
            return out

        return read

    monkeypatch.setattr(credits_spent, "_held_credits_by_key", settle_after_the_first_read(real_held))
    monkeypatch.setattr(credits_spent, "_debited_by_key", settle_after_the_first_read(real_debited))
    async with ledger.factory() as s:
        out = (await credits_spent.spent_by_scope(s, USER, {"x": [scope]}))["x"]
    assert landed == [True]
    assert out["usd"] + out["held_usd"] >= 0.38, out  # counted at least once
    # Holds are read first, so the call shows as held (read 1) and spent (read 2): a double count, not a miss.
    assert (out["held_usd"], out["usd"]) == (pytest.approx(0.40), pytest.approx(0.38))
    # The next read is exact again.
    async with ledger.factory() as s:
        assert (await credits_spent.spent_by_scope(s, USER, {"x": [scope]}))["x"]["held_usd"] == 0


@pytest.mark.asyncio
async def test_the_spent_route_answers_held_usd(ledger, monkeypatch):
    monkeypatch.setenv("INTERNAL_API_SECRET", SECRET)
    app = create_fastapi_app()

    async def _session():
        async with ledger.factory() as s:
            yield s

    app.dependency_overrides[get_session] = _session
    scope = _run()
    await ledger.reserve(scope, "gen-a", HOLD_068)
    headers = {"X-User-ID": USER, "X-Internal-Secret": SECRET}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://store") as c:
        r = await c.post(f"/v1/users/{USER}/credits/spent", headers=headers, json={"scopes": {"p": [scope]}})
    assert r.status_code == 200
    assert r.json()["scopes"]["p"] == {"credits": 0, "usd": 0.0, "held_usd": 0.4, "debit_count": 0}


# ── migration ────────────────────────────────────────────────────────────────


def _load_migration_module():
    spec = importlib.util.spec_from_file_location("migration_0045_chat_productions", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_0045_chains_onto_0044():
    module = _load_migration_module()
    assert module.revision == "0045" and module.down_revision == "0044"


def test_migration_0045_renders_the_table_its_indexes_and_the_action_constraint():
    sql = _render(_load_migration_module().upgrade)
    assert "CREATE TABLE chat_productions" in sql
    # The thread and the owner both cascade, as on every chat table.
    assert "FOREIGN KEY(thread_id) REFERENCES chat_threads (id) ON DELETE CASCADE" in sql
    assert "FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE" in sql
    assert "CHECK (status IN ('open', 'closed'))" in sql
    # At most one open production per thread, and the owner's lists are indexed.
    assert "CREATE UNIQUE INDEX uq_chat_productions_open_thread ON chat_productions (thread_id) WHERE status = 'open'" in sql
    assert "CREATE INDEX ix_chat_productions_user_status ON chat_productions (user_id, status)" in sql
    name = "ck_chat_approvals_action"
    assert f"ALTER TABLE chat_approvals DROP CONSTRAINT {name}" in sql
    assert f"ALTER TABLE chat_approvals ADD CONSTRAINT {name} CHECK (action IN ({ACTIONS_0045}))" in sql
    assert sql.count("DROP CONSTRAINT") == 1 and sql.count("ADD CONSTRAINT") == 1


def test_migration_0045_downgrade_refuses_over_new_approvals_then_drops_the_table():
    sql = _render(_load_migration_module().downgrade)
    assert sql.count("RAISE EXCEPTION") == 1
    assert "action = 'production.budget'" in sql
    assert "downgrade 0045 refused: chat has production.budget approvals; delete them first" in sql
    assert f"CHECK (action IN ({ACTIONS_0044}))" in sql
    assert "DROP TABLE chat_productions" in sql
    assert sql.index("RAISE EXCEPTION") < sql.index("DROP CONSTRAINT") < sql.index("DROP TABLE")


def test_the_models_declare_what_the_migration_makes():
    """Model/migration drift means a future autogenerate "fixes" production."""
    sql = _render(_load_migration_module().upgrade)
    modelled = {
        (t.name, c.name): re.sub(r"\s+", " ", str(c.sqltext))
        for t in (ChatProduction.__table__, ChatApproval.__table__)
        for c in t.constraints
        if getattr(c, "sqltext", None) is not None and c.name
    }
    for name in ("ck_chat_productions_status", "ck_chat_productions_amounts", "ck_chat_productions_ack"):
        assert f"CONSTRAINT {name} CHECK ({modelled[('chat_productions', name)]})" in sql, name
    action = "ck_chat_approvals_action"
    assert f"ADD CONSTRAINT {action} CHECK ({modelled[('chat_approvals', action)]})" in sql
    assert modelled[("chat_approvals", action)] == f"action IN ({ACTIONS_0045})"
    columns = {c.name for c in ChatProduction.__table__.columns}
    assert all(f"{name} " in sql for name in columns), columns
    indexes = {i.name for i in ChatProduction.__table__.indexes}
    assert indexes == {"uq_chat_productions_open_thread", "ix_chat_productions_user_status"}
