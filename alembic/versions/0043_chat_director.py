"""chat_messages.kind and chat_approvals.action: the director's live graph

Revision ID: 0043
Revises: 0042
Create Date: 2026-10-25

The director shows a run as a live graph in the chat. Two new message kinds carry it:
``node`` (one graph node's card, updated as the node moves) and ``event`` (something that
happened on the run). Three new approval actions gate its spends: ``plan.approve``,
``frames.approve`` and ``graph.extend``. Each is a button the user presses; consuming an
approval is unchanged.

Only two CHECK constraints change (0042's ``ck_chat_messages_kind`` and
``ck_chat_approvals_action``): each is dropped and re-created with the longer list.
The downgrade is refused while any row uses a new value, since narrowing the checks would
fail half way over those rows; delete them by hand first.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0043"
down_revision: Union[str, None] = "0042"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_KINDS_0042 = "'text', 'tool', 'approval', 'run', 'media', 'error'"
_KINDS_0043 = f"{_KINDS_0042}, 'node', 'event'"
_ACTIONS_0042 = "'run.start', 'run.continue', 'image.generate', 'clip.render'"
_ACTIONS_0043 = f"{_ACTIONS_0042}, 'plan.approve', 'frames.approve', 'graph.extend'"


def _set_checks(kinds: str, actions: str) -> None:
    op.drop_constraint("ck_chat_messages_kind", "chat_messages", type_="check")
    op.create_check_constraint("ck_chat_messages_kind", "chat_messages", f"kind IN ({kinds})")
    op.drop_constraint("ck_chat_approvals_action", "chat_approvals", type_="check")
    op.create_check_constraint("ck_chat_approvals_action", "chat_approvals", f"action IN ({actions})")


def upgrade() -> None:
    _set_checks(_KINDS_0043, _ACTIONS_0043)


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS (SELECT 1 FROM chat_messages WHERE kind IN ('node', 'event')) "
        "OR EXISTS (SELECT 1 FROM chat_approvals "
        "WHERE action IN ('plan.approve', 'frames.approve', 'graph.extend')) THEN "
        "RAISE EXCEPTION 'downgrade 0043 refused: chat has node or event messages or director approvals; "
        "delete them first'; "
        "END IF; END $$"
    )
    _set_checks(_KINDS_0042, _ACTIONS_0042)
