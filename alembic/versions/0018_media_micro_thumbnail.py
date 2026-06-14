"""add media micro-thumbnail (blur-up placeholder) column

Revision ID: 0018
Revises: 0017
Create Date: 2026-06-14

Additive, column-only migration adding a tiny blur-up placeholder to
``media_items``. ``micro_thumbnail`` is a small ``Text`` column holding a base64
``data:image/webp;base64,…`` URI (~a few hundred bytes) for a ~28px webp. It is
inlined directly in the media list JSON so the grid can paint an instant,
CSS-blurred placeholder under each cell — killing the empty-cell flash — without
any extra HTTP request or new frontend dependency.

The column is nullable: a NULL placeholder falls back to the existing neutral
cell (pre-blur-up behaviour), so existing rows need no backfill job — the store
generates the micro-thumb eagerly when an image's file bytes are stored and
lazily on the first thumbnail GET for legacy rows.

``op.add_column`` of a nullable column is non-locking on Postgres (no table
rewrite, no default backfill) and renders cleanly on the sqlite test harness,
mirroring migration 0017 which added the thumbnail columns the same way.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: Union[str, None] = "0017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "media_items", sa.Column("micro_thumbnail", sa.Text(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("media_items", "micro_thumbnail")
