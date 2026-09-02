"""idea library + clip ratings

Revision ID: 0032
Revises: 0031
Create Date: 2026-09-02

``ideas`` captures every run's input silently at run start (seed) plus the
discussion's approved brief (refined, patched at finalize). ``clip_ratings``
holds exactly one 1-5 verdict + one-line note per clip, linked to its idea via
a nullable FK resolved at rating time (re-resolved on every re-rate, so a
transient failure self-repairs). Both owner-scoped with CASCADE, matching
every existing table.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0032"
down_revision: Union[str, None] = "0031"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ideas",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("seed", sa.Text(), nullable=False),
        sa.Column("refined", sa.Text(), nullable=True),
        sa.Column("template_id", sa.Text(), nullable=False),
        sa.Column("template_name", sa.Text(), nullable=False, server_default=""),
        sa.Column("params", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("run_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_ideas_user_id", "ideas", ["user_id"])
    op.create_index("ix_ideas_user_template", "ideas", ["user_id", "template_id"])
    # One idea per run, enforced by the DB: a duplicate create would otherwise
    # fork the run's history across two rows and the run→idea lookup, the
    # refined patch and the rating link could each land on a different one.
    # Also serves the by-run lookup, so no separate run_id index is needed.
    op.create_index("ux_ideas_user_run", "ideas", ["user_id", "run_id"], unique=True)

    op.create_table(
        "clip_ratings",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "clip_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("clip_prompts.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "idea_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("ideas.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
        # 1-5 stars: the route's pydantic bound only covers the route, and a
        # bad value here would poison the library's AVG for good.
        sa.CheckConstraint("score >= 1 AND score <= 5", name="ck_clip_ratings_score_range"),
    )
    op.create_index("ix_clip_ratings_user_id", "clip_ratings", ["user_id"])
    op.create_index("ix_clip_ratings_idea_id", "clip_ratings", ["idea_id"])


def downgrade() -> None:
    op.drop_table("clip_ratings")
    op.drop_table("ideas")
