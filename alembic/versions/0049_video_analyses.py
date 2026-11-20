"""video_analyses: a long video's analysis, kept 30 days

Revision ID: 0049
Revises: 0048
Create Date: 2026-11-20

One row per (user, platform, video): the transcript (optional: a long video can have no
captions), the signals, the candidate moments and the pick sets made from them. ``expires_at``
is 30 days after the save; no job purges, so readers treat an expired row as absent and a save
deletes the caller's expired rows. The owner owns the rows: the link cascades.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0049"
down_revision: Union[str, None] = "0048"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "video_analyses",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("video_id", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("duration_s", sa.Float(), nullable=False),
        sa.Column("transcript", postgresql.JSONB(), nullable=True),
        sa.Column("signals", postgresql.JSONB(), nullable=False),
        sa.Column("candidates", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("picks", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "platform", "video_id", name="uq_video_analyses_user_platform_video"),
        sa.CheckConstraint("platform IN ('youtube')", name="ck_video_analyses_platform"),
    )


def downgrade() -> None:
    op.drop_table("video_analyses")
