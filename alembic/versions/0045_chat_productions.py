"""chat_productions, and chat_approvals.action gains production.budget

Revision ID: 0045
Revises: 0044
Create Date: 2026-11-02

A production is the chat agent's durable work item for one piece of work: the generation
budget the user approved, the assistant's own cost cap and spend, a log, the artifacts and the
render watches a wake-up resumes from. It belongs to a chat thread and a user, and both links
cascade, as on every chat table. A partial unique index keeps at most one production per
thread ``open``.

The budget is approved on a card whose action is the new ``production.budget``, so
``ck_chat_approvals_action`` is dropped and re-created with the longer list. The downgrade is
refused while any approval row uses the new value, since narrowing the check would fail half
way over those rows; delete them by hand first. It then drops the table.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0045"
down_revision: Union[str, None] = "0044"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ACTIONS_0044 = (
    "'run.start', 'run.continue', 'image.generate', 'clip.render', "
    "'plan.approve', 'frames.approve', 'graph.extend', 'sourcing.run'"
)
_ACTIONS_0045 = f"{_ACTIONS_0044}, 'production.budget'"


def _set_check(actions: str) -> None:
    op.drop_constraint("ck_chat_approvals_action", "chat_approvals", type_="check")
    op.create_check_constraint("ck_chat_approvals_action", "chat_approvals", f"action IN ({actions})")


def upgrade() -> None:
    op.create_table(
        "chat_productions",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("thread_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("chat_threads.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("title", sa.Text(), nullable=False, server_default=""),
        sa.Column("goal", sa.Text(), nullable=False, server_default=""),
        sa.Column("playbook_slug", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="open"),
        sa.Column("budget_usd", sa.Numeric(12, 6), nullable=False, server_default="0"),
        sa.Column("budget_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("agent_cap_usd", sa.Numeric(12, 6), nullable=False, server_default="0"),
        sa.Column("agent_spent_usd", sa.Numeric(12, 6), nullable=False, server_default="0"),
        sa.Column("log", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column(
            "artifacts", postgresql.JSONB(), nullable=False,
            server_default=sa.text("""'{"media_ids": [], "clip_ids": []}'::jsonb"""),
        ),
        sa.Column("watches", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("wake_seq", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("acked_seq", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("wake_reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("outcome", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('open', 'closed')", name="ck_chat_productions_status"),
        sa.CheckConstraint(
            "budget_usd >= 0 AND agent_cap_usd >= 0 AND agent_spent_usd >= 0", name="ck_chat_productions_amounts"
        ),
        sa.CheckConstraint("acked_seq <= wake_seq", name="ck_chat_productions_ack"),
    )
    op.create_index(
        "uq_chat_productions_open_thread", "chat_productions", ["thread_id"], unique=True,
        postgresql_where=sa.text("status = 'open'"),
    )
    op.create_index("ix_chat_productions_user_status", "chat_productions", ["user_id", "status"])
    _set_check(_ACTIONS_0045)


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS (SELECT 1 FROM chat_approvals WHERE action = 'production.budget') THEN "
        "RAISE EXCEPTION 'downgrade 0045 refused: chat has production.budget approvals; delete them first'; "
        "END IF; END $$"
    )
    _set_check(_ACTIONS_0044)
    op.drop_table("chat_productions")
