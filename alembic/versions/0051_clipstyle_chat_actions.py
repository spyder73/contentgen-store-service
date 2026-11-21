"""chat approvals for clip styles built by chat

Revision ID: 0051
Revises: 0050
Create Date: 2026-11-21

An approval may be ``clipstyle.build``, ``clipstyle.edit``, ``clipstyle.save`` or ``clipstyle.publish``.
No table changes.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0051"
down_revision: Union[str, None] = "0050"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ACTIONS_0046 = (
    "'run.start', 'run.continue', 'image.generate', 'clip.render', "
    "'plan.approve', 'frames.approve', 'graph.extend', 'sourcing.run', 'production.budget', 'playbook.save'"
)
_ACTIONS_0050 = f"{_ACTIONS_0046}, 'routine.save', 'routine.delete'"
_ACTIONS_0051 = f"{_ACTIONS_0050}, 'clipstyle.build', 'clipstyle.edit', 'clipstyle.save', 'clipstyle.publish'"


def _set_check(actions: str) -> None:
    op.drop_constraint("ck_chat_approvals_action", "chat_approvals", type_="check")
    op.create_check_constraint("ck_chat_approvals_action", "chat_approvals", f"action IN ({actions})")


def upgrade() -> None:
    _set_check(_ACTIONS_0051)


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS (SELECT 1 FROM chat_approvals WHERE action IN "
        "('clipstyle.build', 'clipstyle.edit', 'clipstyle.save', 'clipstyle.publish')) THEN "
        "RAISE EXCEPTION 'downgrade 0051 refused: chat has clip-style approvals; delete them first'; "
        "END IF; END $$"
    )
    _set_check(_ACTIONS_0050)
