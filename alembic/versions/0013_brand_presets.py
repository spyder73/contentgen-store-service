"""Add user-scoped brand presets."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0013_brand_presets"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "brand_presets",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("clip_style", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("brand_tag", sa.Text(), nullable=False, server_default=""),
        sa.Column("preset_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", "clip_style", "name", name="uq_brand_presets_user_style_name"),
    )
    op.create_index("ix_brand_presets_user_clip_style", "brand_presets", ["user_id", "clip_style"])


def downgrade() -> None:
    op.drop_index("ix_brand_presets_user_clip_style", table_name="brand_presets")
    op.drop_table("brand_presets")
