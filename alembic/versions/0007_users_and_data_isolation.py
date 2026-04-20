"""add users table and user_id columns for data isolation

Revision ID: 0007
Revises: 0006
Create Date: 2026-04-15

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("username", sa.Text(), nullable=False, unique=True),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False, server_default=""),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="TRUE"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # Add user_id to user-scoped tables
    for table in ("clip_prompts", "media_items", "series"):
        op.add_column(
            table,
            sa.Column(
                "user_id",
                sa.dialects.postgresql.UUID(as_uuid=False),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=True,
            ),
        )
        op.create_index(f"ix_{table}_user_id", table, ["user_id"])


def downgrade() -> None:
    for table in ("series", "media_items", "clip_prompts"):
        op.drop_index(f"ix_{table}_user_id", table_name=table)
        op.drop_column(table, "user_id")
    op.drop_table("users")
