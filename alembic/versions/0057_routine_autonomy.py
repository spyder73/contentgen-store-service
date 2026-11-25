"""routine budgets, press choices, chat Telegram targets, Telegram recipients

Revision ID: 0057
Revises: 0056
Create Date: 2026-11-25

A routine's budget is a funded production with an end date (``ends_at``) and the spend categories the owner
ticked (``allowed``; null means the production is not a routine budget). A press of an approval card may carry
the ticked ``choices``. A chat keeps the Telegram ids it sends to (``telegram_targets``), chosen from the
user's durable ``telegram_recipients``. An approval may be ``routine.budget`` or ``telegram.targets``.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0057"
down_revision: Union[str, None] = "0056"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ACTIONS_0046 = (
    "'run.start', 'run.continue', 'image.generate', 'clip.render', "
    "'plan.approve', 'frames.approve', 'graph.extend', 'sourcing.run', 'production.budget', 'playbook.save'"
)
_ACTIONS_0050 = f"{_ACTIONS_0046}, 'routine.save', 'routine.delete'"
_ACTIONS_0051 = f"{_ACTIONS_0050}, 'clipstyle.build', 'clipstyle.edit', 'clipstyle.save', 'clipstyle.publish'"
_ACTIONS_0052 = f"{_ACTIONS_0051}, 'scene.build', 'scene.render'"
_ACTIONS_0057 = f"{_ACTIONS_0052}, 'routine.budget', 'telegram.targets'"


def _set_check(actions: str) -> None:
    op.drop_constraint("ck_chat_approvals_action", "chat_approvals", type_="check")
    op.create_check_constraint("ck_chat_approvals_action", "chat_approvals", f"action IN ({actions})")


def upgrade() -> None:
    op.add_column("chat_productions", sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("chat_productions", sa.Column("allowed", postgresql.JSONB(), nullable=True))
    op.add_column("chat_approvals", sa.Column("choices", postgresql.JSONB(), nullable=True))
    op.add_column(
        "chat_threads",
        sa.Column("telegram_targets", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
    )
    op.create_table(
        "telegram_recipients",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("telegram_id", sa.Text(), nullable=False),
        sa.Column("username", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", "telegram_id", name="uq_telegram_recipients_user_tid"),
    )
    _set_check(_ACTIONS_0057)


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS (SELECT 1 FROM chat_approvals WHERE action IN ('routine.budget', 'telegram.targets')) THEN "
        "RAISE EXCEPTION 'downgrade 0057 refused: chat has routine.budget or telegram.targets approvals; delete them first'; "
        "END IF; END $$"
    )
    _set_check(_ACTIONS_0052)
    op.drop_table("telegram_recipients")
    op.drop_column("chat_threads", "telegram_targets")
    op.drop_column("chat_approvals", "choices")
    op.drop_column("chat_productions", "allowed")
    op.drop_column("chat_productions", "ends_at")
