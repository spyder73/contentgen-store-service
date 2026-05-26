"""render templates and proposals

Revision ID: 0013
Revises: 0012
Create Date: 2026-05-26

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013"
down_revision: Union[str, None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "render_templates",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("kind", sa.Text(), nullable=False, server_default="carousel"),
        sa.Column("source", sa.Text(), nullable=False, server_default="user_saved"),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column(
            "config",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("preview_url", sa.Text(), nullable=True),
        sa.Column("created_from_clip_id", sa.Text(), nullable=True),
        sa.Column("created_from_instruction", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint(
            "source IN ('builtin','user_saved','agent_generated')",
            name="render_templates_source_valid",
        ),
        sa.CheckConstraint(
            "status IN ('draft','active','archived')",
            name="render_templates_status_valid",
        ),
    )
    op.create_index("ix_render_templates_user_id", "render_templates", ["user_id"])
    op.create_index(
        "ix_render_templates_kind_status", "render_templates", ["kind", "status"]
    )

    op.create_table(
        "render_proposals",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("clip_id", sa.Text(), nullable=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("kind", sa.Text(), nullable=False, server_default="carousel_design"),
        sa.Column("status", sa.Text(), nullable=False, server_default="draft"),
        sa.Column("instruction", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "source_template_id",
            sa.Text(),
            sa.ForeignKey("render_templates.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "metadata_patch_json",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "template_config_json",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "preview_output_refs_json",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "validation_report_json",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('draft','validated','approved','rejected','failed')",
            name="render_proposals_status_valid",
        ),
    )
    op.create_index("ix_render_proposals_user_id", "render_proposals", ["user_id"])
    op.create_index("ix_render_proposals_clip_id", "render_proposals", ["clip_id"])
    op.create_index("ix_render_proposals_status", "render_proposals", ["status"])


def downgrade() -> None:
    op.drop_index("ix_render_proposals_status", table_name="render_proposals")
    op.drop_index("ix_render_proposals_clip_id", table_name="render_proposals")
    op.drop_index("ix_render_proposals_user_id", table_name="render_proposals")
    op.drop_table("render_proposals")

    op.drop_index("ix_render_templates_kind_status", table_name="render_templates")
    op.drop_index("ix_render_templates_user_id", table_name="render_templates")
    op.drop_table("render_templates")
