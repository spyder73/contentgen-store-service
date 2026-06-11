"""generator profiles

Revision ID: 0015
Revises: 0014
Create Date: 2026-06-11

Versioned generator profiles (base model + adapters + prompt + params). A slug
groups versions; (slug, version) is unique and a slug holds at most one draft at
a time. Characters gain a nullable FK to a profile. Pure additive migration.

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015"
down_revision: Union[str, None] = "0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "generator_profiles",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=False),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="draft"),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("media_type", sa.Text(), nullable=False, server_default="image"),
        sa.Column(
            "spec",
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
        sa.UniqueConstraint("slug", "version", name="uq_generator_profiles_slug_version"),
        sa.CheckConstraint(
            "status IN ('draft','published')",
            name="generator_profiles_status_valid",
        ),
    )
    op.create_index(
        "ix_generator_profiles_slug_version",
        "generator_profiles",
        ["slug", "version"],
        unique=True,
    )
    op.create_index(
        "ix_generator_profiles_status", "generator_profiles", ["status"]
    )

    op.add_column(
        "characters",
        sa.Column(
            "generator_profile_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("generator_profiles.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("characters", "generator_profile_id")
    op.drop_index("ix_generator_profiles_status", table_name="generator_profiles")
    op.drop_index(
        "ix_generator_profiles_slug_version", table_name="generator_profiles"
    )
    op.drop_table("generator_profiles")
