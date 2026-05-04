"""admin feature access and pipeline assignments

Revision ID: 0012
Revises: 0011
Create Date: 2026-05-04

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


FEATURES = (
    ("studio", False),
    ("generate", True),
    ("series", True),
    ("docs", True),
    ("builder", True),
    ("pipeline_manager", True),
    ("archive", True),
    ("upload_media", True),
)


def upgrade() -> None:
    op.add_column(
        "pipeline_templates",
        sa.Column("visibility", sa.Text(), nullable=False, server_default="private"),
    )
    op.add_column(
        "prompt_templates",
        sa.Column("visibility", sa.Text(), nullable=False, server_default="private"),
    )

    op.create_table(
        "pipeline_template_assignments",
        sa.Column(
            "template_id",
            sa.Text(),
            sa.ForeignKey("pipeline_templates.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "user_id",
            sa.dialects.postgresql.UUID(as_uuid=False),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index(
        "ix_pipeline_template_assignments_user_id",
        "pipeline_template_assignments",
        ["user_id"],
    )

    op.create_table(
        "access_features",
        sa.Column("feature_key", sa.Text(), primary_key=True),
        sa.Column(
            "whitelist_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("TRUE"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
        ),
    )
    op.create_table(
        "access_feature_users",
        sa.Column(
            "feature_key",
            sa.Text(),
            sa.ForeignKey("access_features.feature_key", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "user_id",
            sa.dialects.postgresql.UUID(as_uuid=False),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("feature_key", "user_id", name="uq_access_feature_users_feature_user"),
    )
    op.create_index("ix_access_feature_users_user_id", "access_feature_users", ["user_id"])

    feature_table = sa.table(
        "access_features",
        sa.column("feature_key", sa.Text()),
        sa.column("whitelist_enabled", sa.Boolean()),
    )
    op.bulk_insert(
        feature_table,
        [{"feature_key": key, "whitelist_enabled": enabled} for key, enabled in FEATURES],
    )

    conn = op.get_bind()
    dorian_id = conn.execute(sa.text("SELECT id FROM users WHERE username = 'dorian' LIMIT 1")).scalar()
    if dorian_id:
        conn.execute(
            sa.text(
                "UPDATE pipeline_templates SET user_id = :uid, visibility = 'private' WHERE user_id IS NULL"
            ),
            {"uid": dorian_id},
        )
        conn.execute(
            sa.text(
                "UPDATE prompt_templates SET user_id = :uid, visibility = 'private' WHERE user_id IS NULL"
            ),
            {"uid": dorian_id},
        )
        conn.execute(
            sa.text(
                """
                INSERT INTO access_feature_users(feature_key, user_id)
                SELECT feature_key, :uid
                FROM access_features
                WHERE feature_key <> 'studio'
                ON CONFLICT DO NOTHING
                """
            ),
            {"uid": dorian_id},
        )

        conn.execute(
            sa.text(
                """
                INSERT INTO access_feature_users(feature_key, user_id)
                SELECT af.feature_key, u.id
                FROM access_features af
                CROSS JOIN users u
                WHERE u.is_admin IS TRUE AND af.feature_key <> 'studio'
                ON CONFLICT DO NOTHING
                """
            )
        )


def downgrade() -> None:
    op.drop_index("ix_access_feature_users_user_id", table_name="access_feature_users")
    op.drop_table("access_feature_users")
    op.drop_table("access_features")
    op.drop_index("ix_pipeline_template_assignments_user_id", table_name="pipeline_template_assignments")
    op.drop_table("pipeline_template_assignments")
    op.drop_column("prompt_templates", "visibility")
    op.drop_column("pipeline_templates", "visibility")
