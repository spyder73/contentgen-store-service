"""add media thumbnail derivative columns

Revision ID: 0017
Revises: 0016
Create Date: 2026-06-14

Additive, column-only migration adding a small derived thumbnail to
``media_items`` so the library grid serves a ≤512px webp variant instead of the
full-resolution original BLOB.

``thumbnail_data`` (LargeBinary) holds the encoded thumbnail bytes and
``thumbnail_content_type`` its MIME type. Both are nullable: a NULL thumbnail
falls back to the original (pre-thumbnail behaviour), so existing rows need no
backfill job — the store generates a thumbnail eagerly when an image's file
bytes are stored and lazily on the first thumbnail GET for legacy rows.

``op.add_column`` of a nullable column is non-locking on Postgres (no table
rewrite, no default backfill) and renders cleanly on the sqlite test harness,
mirroring migration 0006 which added ``file_data``/``file_mime_type`` the same
way.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: Union[str, None] = "0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "media_items", sa.Column("thumbnail_data", sa.LargeBinary(), nullable=True)
    )
    op.add_column(
        "media_items", sa.Column("thumbnail_content_type", sa.Text(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("media_items", "thumbnail_content_type")
    op.drop_column("media_items", "thumbnail_data")
