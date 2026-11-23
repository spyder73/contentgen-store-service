"""scene_videos.voice_takes: the narration's per-chunk voice takes

Revision ID: 0053
Revises: 0052
Create Date: 2026-11-23

A scene video's narration is recorded in chunks of lines, one voice take per chunk; the row keeps
each take (``first``, ``last``, ``texts``, ``voice``, ``media_id``) so a re-voice reuses unchanged chunks.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0053"
down_revision: Union[str, None] = "0052"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("scene_videos", sa.Column("voice_takes", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("scene_videos", "voice_takes")
