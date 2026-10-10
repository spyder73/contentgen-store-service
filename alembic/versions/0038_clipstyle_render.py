"""render_templates: clipstyle render (Phase 4) — visibility, frozen render bundles

Revision ID: 0038
Revises: 0037
Create Date: 2026-10-10

Adds the per-version visibility flag (private by default; Phase 4 publishing sets the others) and
the append-only table of frozen render bundles: the compiled webpack bundle the validator built for
an approved package version, per runtime image (Remotion's bundle and renderer versions must
match). Only the bridge writes it; a trigger refuses UPDATE and DELETE.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0038"
down_revision: Union[str, None] = "0037"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("render_templates", sa.Column("visibility", sa.Text(), nullable=False, server_default="private"))
    op.create_check_constraint(
        "render_templates_visibility_valid", "render_templates", "visibility IN ('private','published','revoked')"
    )
    op.create_table(
        "clipstyle_render_bundles",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("template_id", sa.Text(), sa.ForeignKey("render_templates.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("bundle_sha256", sa.Text(), nullable=False),
        sa.Column("media_id", sa.Text(), nullable=False),
        sa.Column("image_ref", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("template_id", "image_ref", name="ux_clipstyle_render_bundles_template_image"),
        sa.CheckConstraint("bundle_sha256 ~ '^[0-9a-f]{64}$'", name="clipstyle_render_bundles_sha_valid"),
    )
    op.execute(
        "CREATE FUNCTION clipstyle_render_bundles_append_only() RETURNS trigger AS $$ "
        "BEGIN RAISE EXCEPTION 'clipstyle_render_bundles is append-only'; END; $$ LANGUAGE plpgsql"
    )
    op.execute(
        "CREATE TRIGGER clipstyle_render_bundles_no_change BEFORE UPDATE OR DELETE ON clipstyle_render_bundles "
        "FOR EACH ROW EXECUTE FUNCTION clipstyle_render_bundles_append_only()"
    )


def downgrade() -> None:
    # Refuse rather than drop frozen bundles or publishing state: resolve them by hand first.
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS (SELECT 1 FROM clipstyle_render_bundles) "
        "OR EXISTS (SELECT 1 FROM render_templates WHERE visibility <> 'private') THEN "
        "RAISE EXCEPTION 'downgrade 0038 refused: frozen render bundles or published/revoked styles exist'; "
        "END IF; END $$"
    )
    op.execute("DROP TRIGGER IF EXISTS clipstyle_render_bundles_no_change ON clipstyle_render_bundles")
    op.execute("DROP FUNCTION IF EXISTS clipstyle_render_bundles_append_only()")
    op.drop_table("clipstyle_render_bundles")
    op.drop_constraint("render_templates_visibility_valid", "render_templates", type_="check")
    op.drop_column("render_templates", "visibility")
