"""clipstyle render Phase 4: frozen brand assets (append-only)

Revision ID: 0039
Revises: 0038
Create Date: 2026-10-16

Adds the append-only table of frozen brand assets: one row per (approved package version, label),
inserted in the approval's own transaction once each asset's media is checked to exist, belong to
the owner and be the hidden `clipstyle_asset` kind. Only the store writes it, at approval; a
trigger refuses UPDATE and DELETE.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0039"
down_revision: Union[str, None] = "0038"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "clipstyle_assets",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("template_id", sa.Text(), sa.ForeignKey("render_templates.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("media_id", sa.Text(), nullable=False),
        sa.Column("mime", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("template_id", "label", name="ux_clipstyle_assets_template_label"),
        sa.CheckConstraint("label IN ('logo','wordmark','texture','background','icon')", name="clipstyle_assets_label_valid"),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="clipstyle_assets_sha_valid"),
        sa.CheckConstraint("mime IN ('image/png','image/jpeg','image/webp')", name="clipstyle_assets_mime_valid"),
    )
    op.create_index("ix_clipstyle_assets_media_id", "clipstyle_assets", ["media_id"])
    op.execute(
        "CREATE FUNCTION clipstyle_assets_append_only() RETURNS trigger AS $$ "
        "BEGIN RAISE EXCEPTION 'clipstyle_assets is append-only'; END; $$ LANGUAGE plpgsql"
    )
    op.execute(
        "CREATE TRIGGER clipstyle_assets_no_change BEFORE UPDATE OR DELETE ON clipstyle_assets "
        "FOR EACH ROW EXECUTE FUNCTION clipstyle_assets_append_only()"
    )


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM clipstyle_assets) THEN "
        "RAISE EXCEPTION 'downgrade 0039 refused: frozen brand assets exist'; END IF; END $$"
    )
    op.execute("DROP TRIGGER IF EXISTS clipstyle_assets_no_change ON clipstyle_assets")
    op.execute("DROP FUNCTION IF EXISTS clipstyle_assets_append_only()")
    op.drop_index("ix_clipstyle_assets_media_id", table_name="clipstyle_assets")
    op.drop_table("clipstyle_assets")
