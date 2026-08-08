"""add media proxy derivative columns

Revision ID: 0030
Revises: 0029
Create Date: 2026-08-08

Additive, column-only migration adding a persisted 480p video proxy
derivative to ``media_items``, mirroring migration 0017's thumbnail columns.

The Go backend's ``/media/proxy/:filename`` route (D5 of the video-editor
plan) transcodes a 480p ffmpeg proxy on cache miss and best-effort uploads it
here so a disk-cache miss on another instance (or after a restart) can be
served from the store instead of re-transcoding. ``proxy_bytes`` holds the
encoded mp4 bytes and ``proxy_mime`` its content type. Both are nullable: a
NULL proxy means "not generated yet" and the caller falls back to the
original — no backfill job needed for existing rows.

``op.add_column`` of a nullable column is non-locking on Postgres (no table
rewrite, no default backfill) and renders cleanly on the sqlite test harness,
same as 0017.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0030"
down_revision: Union[str, None] = "0029"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "media_items", sa.Column("proxy_bytes", sa.LargeBinary(), nullable=True)
    )
    op.add_column(
        "media_items", sa.Column("proxy_mime", sa.Text(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("media_items", "proxy_mime")
    op.drop_column("media_items", "proxy_bytes")
