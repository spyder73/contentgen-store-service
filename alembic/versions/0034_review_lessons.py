"""review_corrections: distilled lessons

Revision ID: 0034
Revises: 0033
Create Date: 2026-09-05

Adds the distilled-lesson columns to ``review_corrections``: ``lesson`` (the
rule the reviewer should learn, distinct from ``reason`` — the user's raw
note), ``reinforces_id`` (an optional self-FK to a prior correction this one
echoes, incrementing that row's ``weight``), and ``weight`` (how many times a
lesson has been reinforced, starting at 1). ``scope`` keeps its existing
column and default ("this_pipeline"); its allowed values widen at the
application layer to this_checkpoint | this_pipeline | all_pipelines with no
data rewrite needed.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0034"
down_revision: Union[str, None] = "0033"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "review_corrections",
        sa.Column("lesson", sa.Text(), nullable=False, server_default=""),
    )
    op.add_column(
        "review_corrections",
        sa.Column(
            "reinforces_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("review_corrections.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "review_corrections",
        sa.Column("weight", sa.Integer(), nullable=False, server_default="1"),
    )


def downgrade() -> None:
    op.drop_column("review_corrections", "weight")
    op.drop_column("review_corrections", "reinforces_id")
    op.drop_column("review_corrections", "lesson")
