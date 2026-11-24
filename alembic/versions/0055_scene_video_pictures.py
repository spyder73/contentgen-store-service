"""scene_videos.pictures: the pictures a scene video uses

Revision ID: 0055
Revises: 0054
Create Date: 2026-11-24

A scene video keeps a list of its pictures (label ``pic-<name>``, a hidden ``scene_picture`` media,
its sha256, mime, size, and optional credit/licence/source), so a check and a render read the same
bytes. Media are referenced by id, no foreign key (as ``voice_takes``).
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0055"
down_revision: Union[str, None] = "0054"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("scene_videos", sa.Column("pictures", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("scene_videos", "pictures")
