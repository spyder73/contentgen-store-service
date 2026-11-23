"""scene_style_examples: the shot plan of a rendered scene video, kept as an example on its style

Revision ID: 0054
Revises: 0053
Create Date: 2026-11-23

One row per scene video (``video_id`` is unique, cascade-deleted with the video). The store keeps
the newest 20 per ``(user_id, style_id)``; the next video in that style reads the nearest ones.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0054"
down_revision: Union[str, None] = "0053"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "scene_style_examples",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("style_id", sa.Text(), nullable=False),
        sa.Column("style_ref", sa.Text(), nullable=False),
        sa.Column(
            "video_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("scene_videos.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("title", sa.Text(), nullable=False, server_default=""),
        sa.Column("topic", sa.Text(), nullable=False, server_default=""),
        sa.Column("plan", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_scene_style_examples_user_style_updated", "scene_style_examples", ["user_id", "style_id", "updated_at"]
    )


def downgrade() -> None:
    op.drop_table("scene_style_examples")
