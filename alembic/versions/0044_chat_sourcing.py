"""chat_approvals.action: the chat's paid searches

Revision ID: 0044
Revises: 0043
Create Date: 2026-10-29

The chat agent can run the paid sourcing tools (``x.discover``, ``media.reference_photo``)
behind an approval card. The card is approved as one new action, ``sourcing.run``, whose
subject is ``tool:<tool>|inputs:<16 hex>``. Consuming an approval is unchanged.

Only 0043's ``ck_chat_approvals_action`` changes: it is dropped and re-created with the longer
list. The downgrade is refused while any row uses the new value, since narrowing the check
would fail half way over those rows; delete them by hand first.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0044"
down_revision: Union[str, None] = "0043"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ACTIONS_0043 = (
    "'run.start', 'run.continue', 'image.generate', 'clip.render', "
    "'plan.approve', 'frames.approve', 'graph.extend'"
)
_ACTIONS_0044 = f"{_ACTIONS_0043}, 'sourcing.run'"


def _set_check(actions: str) -> None:
    op.drop_constraint("ck_chat_approvals_action", "chat_approvals", type_="check")
    op.create_check_constraint("ck_chat_approvals_action", "chat_approvals", f"action IN ({actions})")


def upgrade() -> None:
    _set_check(_ACTIONS_0044)


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS (SELECT 1 FROM chat_approvals WHERE action = 'sourcing.run') THEN "
        "RAISE EXCEPTION 'downgrade 0044 refused: chat has sourcing.run approvals; delete them first'; "
        "END IF; END $$"
    )
    _set_check(_ACTIONS_0043)
