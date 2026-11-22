"""scene_videos and the scene.build / scene.render chat approval actions

Revision ID: 0052
Revises: 0051
Create Date: 2026-11-21

``scene_videos`` holds the code-made 3D explainer videos the chat agent makes (at most 500 per
user, enforced by the store). An approval may be ``scene.build`` or ``scene.render``.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0052"
down_revision: Union[str, None] = "0051"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ACTIONS_0046 = (
    "'run.start', 'run.continue', 'image.generate', 'clip.render', "
    "'plan.approve', 'frames.approve', 'graph.extend', 'sourcing.run', 'production.budget', 'playbook.save'"
)
_ACTIONS_0050 = f"{_ACTIONS_0046}, 'routine.save', 'routine.delete'"
_ACTIONS_0051 = f"{_ACTIONS_0050}, 'clipstyle.build', 'clipstyle.edit', 'clipstyle.save', 'clipstyle.publish'"
_ACTIONS_0052 = f"{_ACTIONS_0051}, 'scene.build', 'scene.render'"


def _set_check(actions: str) -> None:
    op.drop_constraint("ck_chat_approvals_action", "chat_approvals", type_="check")
    op.create_check_constraint("ck_chat_approvals_action", "chat_approvals", f"action IN ({actions})")


def upgrade() -> None:
    op.create_table(
        "scene_videos",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("thread_id", sa.Text(), nullable=True),
        sa.Column("style_ref", sa.Text(), nullable=False, server_default="sceneStarter@1"),
        sa.Column("title", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.Text(), nullable=False, server_default="draft"),
        sa.Column("files", postgresql.JSONB(), nullable=True),
        sa.Column("files_hash", sa.Text(), nullable=True),
        sa.Column("last_report", postgresql.JSONB(), nullable=True),
        sa.Column("bundle", postgresql.JSONB(), nullable=True),
        sa.Column("narration_media_id", sa.Text(), nullable=True),
        sa.Column("voice_timeline", postgresql.JSONB(), nullable=True),
        sa.Column("plan", postgresql.JSONB(), nullable=True),
        sa.Column("final_media_id", sa.Text(), nullable=True),
        sa.Column("session_id", sa.Text(), nullable=True),
        sa.Column("woken_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "status IN ('draft', 'voiced', 'building', 'built', 'rendering', 'ready', 'failed')",
            name="ck_scene_videos_status",
        ),
    )
    op.create_index("ix_scene_videos_user_created", "scene_videos", ["user_id", "created_at"])
    _set_check(_ACTIONS_0052)


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS (SELECT 1 FROM chat_approvals WHERE action IN ('scene.build', 'scene.render')) THEN "
        "RAISE EXCEPTION 'downgrade 0052 refused: chat has scene approvals; delete them first'; "
        "END IF; END $$"
    )
    _set_check(_ACTIONS_0051)
    op.drop_table("scene_videos")
