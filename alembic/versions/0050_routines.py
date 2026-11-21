"""routines: scheduled prompts, their daily spend counter, the thread's unread flag

Revision ID: 0050
Revises: 0049
Create Date: 2026-11-21

``routines`` holds the user's scheduled prompts (at most five each, enforced by the store);
``routine_spend_days`` the per-local-day spend counter their budget reads. ``chat_threads``
gains ``unread`` and ``routine_id``, and an approval may be ``routine.save`` or ``routine.delete``.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0050"
down_revision: Union[str, None] = "0049"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ACTIONS_0046 = (
    "'run.start', 'run.continue', 'image.generate', 'clip.render', "
    "'plan.approve', 'frames.approve', 'graph.extend', 'sourcing.run', 'production.budget', 'playbook.save'"
)
_ACTIONS_0050 = f"{_ACTIONS_0046}, 'routine.save', 'routine.delete'"


def _set_check(actions: str) -> None:
    op.drop_constraint("ck_chat_approvals_action", "chat_approvals", type_="check")
    op.create_check_constraint("ck_chat_approvals_action", "chat_approvals", f"action IN ({actions})")


def upgrade() -> None:
    op.create_table(
        "routines",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("cadence", sa.Text(), nullable=False),
        sa.Column("weekday", sa.Integer(), nullable=True),
        sa.Column("time_of_day", sa.Text(), nullable=False),
        sa.Column("time_zone", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False, server_default="active"),
        sa.Column("thread_id", sa.Text(), nullable=True),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_outcome", sa.Text(), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error_code", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("cadence IN ('daily', 'weekly')", name="ck_routines_cadence"),
        sa.CheckConstraint("state IN ('active', 'paused')", name="ck_routines_state"),
        sa.CheckConstraint("last_outcome IS NULL OR last_outcome IN ('ok', 'failed', 'skipped')", name="ck_routines_outcome"),
    )
    op.create_index("ix_routines_state_next_run", "routines", ["state", "next_run_at"])
    op.create_table(
        "routine_spend_days",
        sa.Column("user_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("spent_usd", sa.Numeric(10, 4), nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("user_id", "day"),
    )
    op.add_column("chat_threads", sa.Column("unread", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("chat_threads", sa.Column("routine_id", sa.Text(), nullable=True))
    _set_check(_ACTIONS_0050)


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS (SELECT 1 FROM chat_approvals WHERE action IN ('routine.save', 'routine.delete')) THEN "
        "RAISE EXCEPTION 'downgrade 0050 refused: chat has routine approvals; delete them first'; "
        "END IF; END $$"
    )
    _set_check(_ACTIONS_0046)
    op.drop_column("chat_threads", "routine_id")
    op.drop_column("chat_threads", "unread")
    op.drop_table("routine_spend_days")
    op.drop_index("ix_routines_state_next_run", table_name="routines")
    op.drop_table("routines")
