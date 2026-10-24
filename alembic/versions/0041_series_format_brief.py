"""series.format_brief: the per-series format brief the crew agents read

Revision ID: 0041
Revises: 0040
Create Date: 2026-10-24

Adds ``series.format_brief TEXT NOT NULL DEFAULT ''``. The brief is the only
place a show's genre and style live; the engine and the crew's built-in prompts
carry none. The server default backfills existing rows with ``''`` and lets a
writer that never heard of the column keep inserting.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0041"
down_revision: Union[str, None] = "0040"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "series",
        sa.Column("format_brief", sa.Text(), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("series", "format_brief")
