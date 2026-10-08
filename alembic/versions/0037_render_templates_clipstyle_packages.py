"""render_templates: clipstyle package lifecycle

Revision ID: 0037
Revises: 0036
Create Date: 2026-09-28

Adds the validation report + approval timestamp, widens the status CHECK for the package
lifecycle (draft -> validated -> approved | rejected), and guarantees one approved row per
package id@version.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0037"
down_revision: Union[str, None] = "0036"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_INDEX = "ux_render_templates_approved_package_version"


def upgrade() -> None:
    op.drop_constraint("render_templates_status_valid", "render_templates", type_="check")
    op.create_check_constraint(
        "render_templates_status_valid",
        "render_templates",
        "status IN ('draft','active','archived','validated','approved','rejected')",
    )
    op.add_column(
        "render_templates",
        sa.Column("validation_report_json", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
    )
    op.add_column("render_templates", sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True))
    op.execute(
        f"CREATE UNIQUE INDEX {_INDEX} ON render_templates "
        "((config->'manifest'->>'id'), (config->'manifest'->>'version')) "
        "WHERE kind = 'clipstyle_package' AND status = 'approved'"
    )


def downgrade() -> None:
    # Refuse rather than silently erase approval state: the narrowed CHECK cannot hold the package
    # lifecycle statuses. Resolve (or delete) those rows by hand first, then downgrade.
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS (SELECT 1 FROM render_templates WHERE status IN ('validated','approved','rejected')) THEN "
        "RAISE EXCEPTION 'downgrade 0037 refused: render_templates has clipstyle package rows in "
        "validated/approved/rejected; resolve them first (downgrading would erase approval state)'; "
        "END IF; END $$"
    )
    op.execute(f"DROP INDEX IF EXISTS {_INDEX}")
    op.drop_column("render_templates", "approved_at")
    op.drop_column("render_templates", "validation_report_json")
    op.drop_constraint("render_templates_status_valid", "render_templates", type_="check")
    op.create_check_constraint("render_templates_status_valid", "render_templates", "status IN ('draft','active','archived')")
