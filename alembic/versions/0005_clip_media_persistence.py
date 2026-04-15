"""add clip and media persistence columns

Revision ID: 0005
Revises: 0004
Create Date: 2026-04-07

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── clip_prompts new columns ─────────────────────────────────────────────
    op.add_column(
        "clip_prompts",
        sa.Column(
            "media_refs",
            JSONB,
            nullable=False,
            server_default=sa.text("'{\"images\":[],\"ai_videos\":[],\"audios\":[]}'::jsonb"),
        ),
    )
    op.add_column(
        "clip_prompts",
        sa.Column(
            "render_output_urls",
            JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "clip_prompts",
        sa.Column(
            "is_dirty",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("FALSE"),
        ),
    )
    op.add_column(
        "clip_prompts",
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "clip_prompts",
        sa.Column("thumbnail_url", sa.Text(), nullable=True),
    )

    # ── media_items new columns ──────────────────────────────────────────────
    op.add_column(
        "media_items",
        sa.Column("name", sa.Text(), nullable=False, server_default=sa.text("''")),
    )
    op.add_column(
        "media_items",
        sa.Column("pipeline_run_id", sa.UUID(), nullable=True),
    )
    op.add_column(
        "media_items",
        sa.Column("scene_id", sa.Text(), nullable=True),
    )
    op.add_column(
        "media_items",
        sa.Column(
            "parent_media_id",
            sa.UUID(),
            sa.ForeignKey("media_items.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "media_items",
        sa.Column("role", sa.Text(), nullable=True),
    )

    # ── media_items indexes ──────────────────────────────────────────────────
    op.create_index("ix_media_items_pipeline_run_id", "media_items", ["pipeline_run_id"])
    op.create_index("ix_media_items_scene_id", "media_items", ["scene_id"])
    op.create_index("ix_media_items_name", "media_items", ["name"])
    op.create_index(
        "ix_media_items_pipeline_scene_type",
        "media_items",
        ["pipeline_run_id", "scene_id", "type"],
    )

    # ── backfill name for existing rows ─────────────────────────────────────
    op.execute(
        sa.text(
            "UPDATE media_items SET name = CONCAT(type, '-', LEFT(id::text, 8)) WHERE name = ''"
        )
    )


def downgrade() -> None:
    op.drop_index("ix_media_items_pipeline_scene_type", table_name="media_items")
    op.drop_index("ix_media_items_name", table_name="media_items")
    op.drop_index("ix_media_items_scene_id", table_name="media_items")
    op.drop_index("ix_media_items_pipeline_run_id", table_name="media_items")

    op.drop_column("media_items", "role")
    op.drop_column("media_items", "parent_media_id")
    op.drop_column("media_items", "scene_id")
    op.drop_column("media_items", "pipeline_run_id")
    op.drop_column("media_items", "name")

    op.drop_column("clip_prompts", "thumbnail_url")
    op.drop_column("clip_prompts", "finished_at")
    op.drop_column("clip_prompts", "is_dirty")
    op.drop_column("clip_prompts", "render_output_urls")
    op.drop_column("clip_prompts", "media_refs")
