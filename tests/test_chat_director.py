"""Director chat: ``node`` and ``event`` messages and the plan / frames / extend approvals (migration 0043).

Migration 0042 fixed the chat's message kinds and approval actions with two CHECK constraints.
The director's live graph writes two more message kinds (``node``: one graph node's card,
``event``: a thing that happened on the run) and asks for three more kinds of button-approved
spend (``plan.approve``, ``frames.approve``, ``graph.extend``). Consume semantics are
unchanged: a token still pays once, for the thread, action and subject it was approved for.

The behaviour tests reuse the real-app harness of ``test_chat.py`` (a sqlite database built from
the models, so the model constraints are enforced). The migration's DDL is asserted by
rendering it offline in the postgres dialect.
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

from app.models import ChatApproval, ChatMessage
from tests.test_chat import Env, _consume_body, _h, _render, env  # noqa: F401  (env is the fixture)

MIGRATIONS = Path(__file__).resolve().parents[1] / "alembic" / "versions"
MIGRATION_PATH = MIGRATIONS / "0043_chat_director.py"

DIRECTOR_ACTIONS = ("plan.approve", "frames.approve", "graph.extend")
KINDS_0042 = "'text', 'tool', 'approval', 'run', 'media', 'error'"
KINDS_0043 = f"{KINDS_0042}, 'node', 'event'"
ACTIONS_0042 = "'run.start', 'run.continue', 'image.generate', 'clip.render'"
ACTIONS_0043 = f"{ACTIONS_0042}, 'plan.approve', 'frames.approve', 'graph.extend'"


# ── messages ─────────────────────────────────────────────────────────────────


def test_a_node_and_an_event_message_can_be_saved(env: Env):
    t = env.thread()
    node = env.message(t["id"], role="operator", kind="node", text="", data={"id": "n1", "state": "running"})
    event = env.message(t["id"], role="operator", kind="event", text="Plan approved", data={"name": "plan.approved"})
    assert (node["kind"], node["data"]) == ("node", {"id": "n1", "state": "running"})
    assert (event["kind"], event["text"], event["data"]) == ("event", "Plan approved", {"name": "plan.approved"})
    assert (node["seq"], event["seq"]) == (1, 2)
    listed = env.client.get(f"/v1/chat/threads/{t['id']}/messages", headers=_h()).json()
    assert [(m["seq"], m["kind"]) for m in listed] == [(1, "node"), (2, "event")]
    # A node's card is updated in place as the node moves; its kind and seq stay.
    patched = env.client.patch(f"/v1/chat/messages/{node['id']}", json={"data": {"id": "n1", "state": "done"}}, headers=_h())
    assert patched.status_code == 200, patched.text
    assert (patched.json()["kind"], patched.json()["seq"], patched.json()["data"]) == ("node", 1, {"id": "n1", "state": "done"})
    assert env.count(ChatMessage, ChatMessage.kind.in_(["node", "event"])) == 2


# ── approvals ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("action", DIRECTOR_ACTIONS)
def test_the_three_director_actions_can_be_approved_and_consumed(env: Env, action: str):
    thread_id, approval_id, token = env.approved(action=action, subject="run-1", amount_usd=2.0)
    stored = env.client.get(f"/v1/chat/approvals/{approval_id}", headers=_h()).json()
    assert (stored["action"], stored["status"]) == (action, "approved")
    body = _consume_body(thread_id, token, action=action, subject="run-1", needed_usd=2.0)
    r = env.consume(body)
    assert r.status_code == 200, r.text
    assert (r.json()["status"], r.json()["id"], r.json()["action"]) == ("used", approval_id, action)
    # One time, like every other approval.
    again = env.consume(body)
    assert (again.status_code, again.json()["detail"]) == (409, "used")
    assert env.count(ChatApproval, ChatApproval.action == action) == 1


def test_a_director_approval_pays_only_for_its_own_action_and_subject(env: Env):
    thread_id, _, token = env.approved(action="plan.approve", subject="run-1")
    for over, reason in (({"action": "frames.approve"}, "wrong_action"), ({"subject": "run-2"}, "wrong_subject")):
        r = env.consume(_consume_body(thread_id, token, **{"action": "plan.approve", "subject": "run-1", **over}))
        assert (r.status_code, r.json()["detail"]) == (409, reason)
    # The refusals spent nothing: the approval still pays for what it was approved for.
    ok = env.consume(_consume_body(thread_id, token, action="plan.approve", subject="run-1"))
    assert ok.status_code == 200


# ── still closed ─────────────────────────────────────────────────────────────


def test_an_unknown_kind_is_still_422(env: Env):
    t = env.thread()
    url = f"/v1/chat/threads/{t['id']}/messages"
    for kind in ("poem", "nodes", "Node", "graph"):
        assert env.client.post(url, json={"role": "user", "kind": kind}, headers=_h()).status_code == 422, kind
    assert env.count(ChatMessage) == 0


def test_an_unknown_action_is_still_422(env: Env):
    t = env.thread()
    url = f"/v1/chat/threads/{t['id']}/approvals"
    for action in ("run.delete", "plan.approved", "graph.remove", "frames"):
        body = {"action": action, "subject": "s", "amount_usd": 1}
        assert env.client.post(url, json=body, headers=_h()).status_code == 422, action
    assert env.count(ChatApproval) == 0


# ── migration ────────────────────────────────────────────────────────────────


def _load_migration_module():
    spec = importlib.util.spec_from_file_location("migration_0043_chat_director", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_0043_chains_onto_0042():
    module = _load_migration_module()
    assert module.revision == "0043" and module.down_revision == "0042"


def test_migration_0043_renders_both_constraints():
    sql = _render(_load_migration_module().upgrade)
    assert "'node', 'event'" in sql and "'graph.extend'" in sql
    for table, name, column, values in (
        ("chat_messages", "ck_chat_messages_kind", "kind", KINDS_0043),
        ("chat_approvals", "ck_chat_approvals_action", "action", ACTIONS_0043),
    ):
        assert f"ALTER TABLE {table} DROP CONSTRAINT {name}" in sql, name
        assert f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({column} IN ({values}))" in sql, name
        assert sql.index(f"DROP CONSTRAINT {name}") < sql.index(f"ADD CONSTRAINT {name}")
    # It only touches those two constraints: the status, role and amount checks stay as 0042 made them.
    assert sql.count("DROP CONSTRAINT") == 2 and sql.count("ADD CONSTRAINT") == 2


def test_migration_0043_downgrade_restores_the_0042_lists_and_refuses_over_new_rows():
    sql = _render(_load_migration_module().downgrade)
    assert f"CHECK (kind IN ({KINDS_0042}))" in sql and f"CHECK (action IN ({ACTIONS_0042}))" in sql
    # Narrowing the checks over rows that use the new values would fail half way; it is refused up front.
    assert sql.count("RAISE EXCEPTION") == 1
    assert "kind IN ('node', 'event')" in sql
    assert "action IN ('plan.approve', 'frames.approve', 'graph.extend')" in sql
    assert sql.index("RAISE EXCEPTION") < sql.index("DROP CONSTRAINT")


def test_the_models_declare_what_the_migration_makes():
    """Model/migration drift means a future autogenerate "fixes" production."""
    modelled = {
        c.name: re.sub(r"\s+", " ", str(c.sqltext))
        for t in (ChatMessage, ChatApproval)
        for c in t.__table__.constraints
        if getattr(c, "sqltext", None) is not None and c.name
    }
    sql = _render(_load_migration_module().upgrade)
    for name in ("ck_chat_messages_kind", "ck_chat_approvals_action"):
        assert f"ADD CONSTRAINT {name} CHECK ({modelled[name]})" in sql, name
    assert modelled["ck_chat_messages_kind"] == f"kind IN ({KINDS_0043})"
    assert modelled["ck_chat_approvals_action"] == f"action IN ({ACTIONS_0043})"
