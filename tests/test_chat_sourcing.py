"""The ``sourcing.run`` approval: the chat's paid searches (migration 0044).

Migration 0043 fixed the chat's approval actions with one CHECK constraint. The chat agent can
now run the paid sourcing tools (``x.discover``, ``media.reference_photo``) behind an approval
card, so the store takes one more action, ``sourcing.run``. Consume semantics are unchanged: a
token still pays once, for the thread, action and subject it was approved for. The subject names
the tool and a hash of its inputs (``tool:<tool>|inputs:<16 hex>``), so one card pays for exactly
that call.

The behaviour tests reuse the real-app harness of ``test_chat.py`` (a sqlite database built from
the models, so the model constraints are enforced). The migration's DDL is asserted by rendering
it offline in the postgres dialect.
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

from app.models import ChatApproval
from tests.test_chat import Env, _consume_body, _h, _render, env  # noqa: F401  (env is the fixture)

MIGRATIONS = Path(__file__).resolve().parents[1] / "alembic" / "versions"
MIGRATION_PATH = MIGRATIONS / "0044_chat_sourcing.py"

ACTIONS_0043 = "'run.start', 'run.continue', 'image.generate', 'clip.render', 'plan.approve', 'frames.approve', 'graph.extend'"
ACTIONS_0044 = f"{ACTIONS_0043}, 'sourcing.run'"

SUBJECT = "tool:x.discover|inputs:0123456789abcdef"


# ── approvals ────────────────────────────────────────────────────────────────


def test_a_sourcing_approval_can_be_approved_and_consumed(env: Env):
    thread_id, approval_id, token = env.approved(action="sourcing.run", subject=SUBJECT, amount_usd=0.23)
    stored = env.client.get(f"/v1/chat/approvals/{approval_id}", headers=_h()).json()
    assert (stored["action"], stored["status"], stored["subject"]) == ("sourcing.run", "approved", SUBJECT)
    body = _consume_body(thread_id, token, action="sourcing.run", subject=SUBJECT, needed_usd=0.23)
    r = env.consume(body)
    assert r.status_code == 200, r.text
    assert (r.json()["status"], r.json()["id"], r.json()["action"]) == ("used", approval_id, "sourcing.run")
    # One time, like every other approval.
    again = env.consume(body)
    assert (again.status_code, again.json()["detail"]) == (409, "used")
    assert env.count(ChatApproval, ChatApproval.action == "sourcing.run") == 1


def test_a_sourcing_approval_pays_only_for_its_own_action_and_subject(env: Env):
    thread_id, _, token = env.approved(action="sourcing.run", subject=SUBJECT, amount_usd=0.23)
    other_call = "tool:x.discover|inputs:fedcba9876543210"
    for over, reason in (({"subject": other_call}, "wrong_subject"), ({"action": "image.generate"}, "wrong_action")):
        r = env.consume(_consume_body(thread_id, token, **{"action": "sourcing.run", "subject": SUBJECT, **over}))
        assert (r.status_code, r.json()["detail"]) == (409, reason)
    # The refusals spent nothing: the approval still pays for what it was approved for.
    ok = env.consume(_consume_body(thread_id, token, action="sourcing.run", subject=SUBJECT, needed_usd=0.23))
    assert ok.status_code == 200


# ── migration ────────────────────────────────────────────────────────────────


def _load_migration_module():
    spec = importlib.util.spec_from_file_location("migration_0044_chat_sourcing", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_0044_chains_onto_0043():
    module = _load_migration_module()
    assert module.revision == "0044" and module.down_revision == "0043"


def test_migration_0044_renders_the_action_constraint():
    sql = _render(_load_migration_module().upgrade)
    name = "ck_chat_approvals_action"
    assert f"ALTER TABLE chat_approvals DROP CONSTRAINT {name}" in sql
    assert f"ALTER TABLE chat_approvals ADD CONSTRAINT {name} CHECK (action IN ({ACTIONS_0044}))" in sql
    assert sql.index(f"DROP CONSTRAINT {name}") < sql.index(f"ADD CONSTRAINT {name}")
    # It only touches that one constraint: the message kinds, status and amount checks stay as they were.
    assert sql.count("DROP CONSTRAINT") == 1 and sql.count("ADD CONSTRAINT") == 1


def test_migration_0044_downgrade_restores_0043_and_refuses_over_new_rows():
    sql = _render(_load_migration_module().downgrade)
    assert f"CHECK (action IN ({ACTIONS_0043}))" in sql
    # Narrowing the check over rows that use the new action would fail half way; it is refused up front.
    assert sql.count("RAISE EXCEPTION") == 1
    assert "action = 'sourcing.run'" in sql
    assert "downgrade 0044 refused: chat has sourcing.run approvals; delete them first" in sql
    assert sql.index("RAISE EXCEPTION") < sql.index("DROP CONSTRAINT")


def test_the_models_declare_what_the_migrations_make():
    """Model/migration drift means a future autogenerate "fixes" production."""
    modelled = {
        c.name: re.sub(r"\s+", " ", str(c.sqltext))
        for c in ChatApproval.__table__.constraints
        if getattr(c, "sqltext", None) is not None and c.name
    }
    sql = _render(_load_migration_module().upgrade)
    name = "ck_chat_approvals_action"
    assert f"ADD CONSTRAINT {name} CHECK ({modelled[name]})" in sql
    assert modelled[name] == f"action IN ({ACTIONS_0044})"
