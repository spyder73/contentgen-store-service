"""pipeline run snapshots

Revision ID: 0014
Revises: 0013
Create Date: 2026-06-11

Persist pipeline run snapshots so the Go backend can rehydrate runs after a
restart. Without this, the frontend's stored run IDs 404 on regenerate.

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014"
down_revision: Union[str, None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "pipeline_run_snapshots",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("status", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "snapshot",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_pipeline_run_snapshots_user_id", "pipeline_run_snapshots", ["user_id"]
    )
    op.create_index(
        "ix_pipeline_run_snapshots_status", "pipeline_run_snapshots", ["status"]
    )


def downgrade() -> None:
    op.drop_index("ix_pipeline_run_snapshots_status", table_name="pipeline_run_snapshots")
    op.drop_index("ix_pipeline_run_snapshots_user_id", table_name="pipeline_run_snapshots")
    op.drop_table("pipeline_run_snapshots")
