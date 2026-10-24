"""chat_threads, chat_messages, chat_approvals: the operator chat

Revision ID: 0042
Revises: 0041
Create Date: 2026-10-24

``chat_threads`` is one operator conversation (the transcript the operator resumes
from, the media it knows, the running cost). ``chat_messages`` is the visible log,
numbered per thread by the store. ``chat_approvals`` is a spend the user approves
with a button: only the sha256 of the button's token is stored, and it can be
consumed once. The thread belongs to a user; messages and approvals belong to their
thread. Both links cascade, matching every existing table.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0042"
down_revision: Union[str, None] = "0041"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "chat_threads",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("title", sa.Text(), nullable=False, server_default=""),
        sa.Column("provider", sa.Text(), nullable=False, server_default=""),
        sa.Column("model", sa.Text(), nullable=False, server_default=""),
        sa.Column("series_id", postgresql.UUID(as_uuid=False), nullable=True),
        sa.Column("transcript", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("known_media", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("cost_usd", sa.Numeric(12, 6), nullable=False, server_default="0"),
        sa.Column("status", sa.Text(), nullable=False, server_default="idle"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("status IN ('idle', 'working')", name="ck_chat_threads_status"),
    )
    op.create_index("ix_chat_threads_user_updated", "chat_threads", ["user_id", "updated_at"])

    op.create_table(
        "chat_messages",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("thread_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("chat_threads.id", ondelete="CASCADE"), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False, server_default="text"),
        sa.Column("text", sa.Text(), nullable=False, server_default=""),
        sa.Column("data", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("thread_id", "seq", name="ux_chat_messages_thread_seq"),
        sa.CheckConstraint("role IN ('user', 'operator')", name="ck_chat_messages_role"),
        sa.CheckConstraint(
            "kind IN ('text', 'tool', 'approval', 'run', 'media', 'error')", name="ck_chat_messages_kind"
        ),
    )

    op.create_table(
        "chat_approvals",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("thread_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("chat_threads.id", ondelete="CASCADE"), nullable=False),
        sa.Column("message_id", postgresql.UUID(as_uuid=False), nullable=True),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False, server_default=""),
        sa.Column("amount_usd", sa.Numeric(12, 6), nullable=False, server_default="0"),
        sa.Column("summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("breakdown", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("token_sha256", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "action IN ('run.start', 'run.continue', 'image.generate', 'clip.render')",
            name="ck_chat_approvals_action",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'used', 'declined', 'expired')",
            name="ck_chat_approvals_status",
        ),
        sa.CheckConstraint("amount_usd >= 0", name="ck_chat_approvals_amount"),
    )
    op.create_index("ix_chat_approvals_thread", "chat_approvals", ["thread_id"])
    # One hash backs one approval per thread (NULL while pending, and NULLs are distinct).
    op.create_index(
        "ux_chat_approvals_thread_token", "chat_approvals", ["thread_id", "token_sha256"], unique=True
    )


def downgrade() -> None:
    op.drop_table("chat_approvals")
    op.drop_table("chat_messages")
    op.drop_table("chat_threads")
