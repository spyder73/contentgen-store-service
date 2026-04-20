"""user_api_keys table for per-user provider credentials

Revision ID: 0008
Revises: 0007
Create Date: 2026-04-16

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "user_api_keys",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=False),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "user_id",
            sa.dialects.postgresql.UUID(as_uuid=False),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider", sa.Text(), nullable=False),  # runware | openrouter | suno
        sa.Column("encrypted_key", sa.LargeBinary(), nullable=False),
        sa.Column("last_four", sa.Text(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("user_id", "provider", name="uq_user_api_keys_user_provider"),
    )
    op.create_index("idx_user_api_keys_user_id", "user_api_keys", ["user_id"])


def downgrade() -> None:
    op.drop_index("idx_user_api_keys_user_id", table_name="user_api_keys")
    op.drop_table("user_api_keys")
