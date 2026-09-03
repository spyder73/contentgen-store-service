"""review_traces + review_corrections

Revision ID: 0033
Revises: 0032
Create Date: 2026-09-03

``review_traces`` is an append-only log of every check-tier call the
reviewer made (one row per attempt), caller-minted id so the Go backend's
own retry is idempotent — a second POST with the same id returns the row
already stored rather than forking history. ``review_corrections`` is the
much smaller table of human rulings on a verdict, upserted on
(user_id, verdict_id) so re-judging a verdict updates the existing ruling
in place. Both owner-scoped with CASCADE, matching every existing table.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0033"
down_revision: Union[str, None] = "0032"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "review_traces",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("template_id", sa.Text(), nullable=False, server_default=""),
        sa.Column("checkpoint_id", sa.Text(), nullable=False),
        sa.Column("checkpoint_index", sa.Integer(), nullable=False),
        sa.Column("verdict_id", postgresql.UUID(as_uuid=False), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tier", sa.Text(), nullable=False),
        sa.Column("frame", sa.Text(), nullable=False, server_default=""),
        sa.Column("candidate_id", sa.Text(), nullable=False, server_default=""),
        sa.Column("provider", sa.Text(), nullable=False, server_default=""),
        sa.Column("model", sa.Text(), nullable=False, server_default=""),
        sa.Column("system_prompt", sa.Text(), nullable=False, server_default=""),
        sa.Column("raw_output", sa.Text(), nullable=False, server_default=""),
        sa.Column("images", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("prompt_chars", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("prompt_hash", sa.Text(), nullable=False, server_default=""),
        sa.Column("latency_ms", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("outcome", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_review_traces_run", "review_traces", ["run_id"])
    op.create_index("ix_review_traces_user_created", "review_traces", ["user_id", "created_at"])
    op.create_index("ix_review_traces_verdict", "review_traces", ["verdict_id"])

    op.create_table(
        "review_corrections",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("verdict_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("trace_id", postgresql.UUID(as_uuid=False), nullable=True),
        sa.Column("template_id", sa.Text(), nullable=False),
        sa.Column("checkpoint_id", sa.Text(), nullable=False),
        sa.Column("tier", sa.Text(), nullable=False, server_default=""),
        sa.Column("frame", sa.Text(), nullable=False, server_default=""),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("scope", sa.Text(), nullable=False, server_default="this_pipeline"),
        sa.Column("source", sa.Text(), nullable=False, server_default="user"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint("label IN ('false_pass','false_fail','correct')", name="ck_review_corrections_label"),
    )
    op.create_index("ux_review_corrections_user_verdict", "review_corrections", ["user_id", "verdict_id"], unique=True)
    op.create_index("ix_review_corrections_user_template_created", "review_corrections", ["user_id", "template_id", "created_at"])


def downgrade() -> None:
    op.drop_table("review_corrections")
    op.drop_table("review_traces")
