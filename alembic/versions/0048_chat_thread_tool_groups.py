"""chat_threads.tool_groups: the optional tool groups a thread's agent has loaded

Revision ID: 0048
Revises: 0047
Create Date: 2026-11-20

A JSONB list of group names (media, clips, research; never core). Existing threads read ``[]``.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0048"
down_revision: Union[str, None] = "0047"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "chat_threads",
        sa.Column("tool_groups", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
    )


def downgrade() -> None:
    op.drop_column("chat_threads", "tool_groups")
