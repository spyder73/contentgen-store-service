"""playbooks and playbook_versions, and chat_approvals.action gains playbook.save

Revision ID: 0046
Revises: 0045
Create Date: 2026-11-02

A playbook is a Markdown how-to the chat agent follows. The shipped ones are files in the
backend; a row here is a user's own copy or edit of one (``(user_id, slug)`` is unique) and
overrides the shipped file for its owner. ``version`` counts the saves; ``based_on_shipped_version``
is the shipped version a copy started from. Every save also appends a ``playbook_versions`` row,
which is never updated. The owner owns the playbooks and a playbook owns its versions: both links
cascade, as on every user-scoped table.

The agent's edits wait behind a card whose action is the new ``playbook.save``, so
``ck_chat_approvals_action`` is dropped and re-created with the longer list. The downgrade is
refused while any approval row uses the new value, since narrowing the check would fail half way
over those rows; delete them by hand first. It then drops the two tables.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0046"
down_revision: Union[str, None] = "0045"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ACTIONS_0045 = (
    "'run.start', 'run.continue', 'image.generate', 'clip.render', "
    "'plan.approve', 'frames.approve', 'graph.extend', 'sourcing.run', 'production.budget'"
)
_ACTIONS_0046 = f"{_ACTIONS_0045}, 'playbook.save'"


def _set_check(actions: str) -> None:
    op.drop_constraint("ck_chat_approvals_action", "chat_approvals", type_="check")
    op.create_check_constraint("ck_chat_approvals_action", "chat_approvals", f"action IN ({actions})")


def upgrade() -> None:
    op.create_table(
        "playbooks",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("body", sa.Text(), nullable=False, server_default=""),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("based_on_shipped_version", sa.Integer(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", "slug", name="uq_playbooks_user_slug"),
        sa.CheckConstraint("status IN ('active', 'archived')", name="ck_playbooks_status"),
        sa.CheckConstraint("version >= 1", name="ck_playbooks_version"),
    )
    op.create_table(
        "playbook_versions",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("playbook_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("playbooks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False, server_default=""),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("author", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("playbook_id", "version", name="uq_playbook_versions_playbook_version"),
        sa.CheckConstraint("author IN ('user', 'agent')", name="ck_playbook_versions_author"),
        sa.CheckConstraint("version >= 1", name="ck_playbook_versions_version"),
    )
    _set_check(_ACTIONS_0046)


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS (SELECT 1 FROM chat_approvals WHERE action = 'playbook.save') THEN "
        "RAISE EXCEPTION 'downgrade 0046 refused: chat has playbook.save approvals; delete them first'; "
        "END IF; END $$"
    )
    _set_check(_ACTIONS_0045)
    op.drop_table("playbook_versions")
    op.drop_table("playbooks")
