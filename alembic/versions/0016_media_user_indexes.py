"""media library list indexes (user_id, created_at desc) + (user_id, is_favourite)

Revision ID: 0016
Revises: 0015
Create Date: 2026-06-13

Additive, index-only migration for the media library list access pattern
(`WHERE user_id=? ORDER BY created_at DESC LIMIT ? OFFSET ?`) and the
favourites filter (`WHERE user_id=? AND is_favourite=?`).

On Postgres the indexes are built with ``CREATE INDEX CONCURRENTLY`` so the
live ``media_items`` table is never locked. CONCURRENTLY cannot run inside a
transaction, and alembic's ``env.py`` wraps each migration in one, so the
statements are emitted inside ``op.get_context().autocommit_block()`` which
suspends the surrounding transaction for the duration of the block.

``IF NOT EXISTS`` / ``IF EXISTS`` make the migration idempotent: a partially
applied CONCURRENTLY index (CONCURRENTLY is not atomic and can leave an INVALID
index behind on failure) can be re-run safely.

On non-Postgres dialects (the sqlite test harness) CONCURRENTLY and the DESC
operator class are unsupported, so plain indexes are created via the dialect's
ordinary (transactional) DDL — the access pattern is identical, only the online
guarantee differs.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: Union[str, None] = "0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_COMPOSITE_INDEX = "ix_media_items_user_created_desc"
_FAVOURITE_INDEX = "ix_media_items_user_favourite"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # CONCURRENTLY must run outside a transaction block.
        with op.get_context().autocommit_block():
            op.execute(
                f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_COMPOSITE_INDEX} "
                "ON media_items (user_id, created_at DESC)"
            )
            op.execute(
                f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_FAVOURITE_INDEX} "
                "ON media_items (user_id, is_favourite)"
            )
    else:
        # sqlite (test harness) and other dialects: ordinary transactional DDL.
        op.create_index(
            _COMPOSITE_INDEX,
            "media_items",
            ["user_id", sa.text("created_at DESC")],
            if_not_exists=True,
        )
        op.create_index(
            _FAVOURITE_INDEX,
            "media_items",
            ["user_id", "is_favourite"],
            if_not_exists=True,
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute(
                f"DROP INDEX CONCURRENTLY IF EXISTS {_FAVOURITE_INDEX}"
            )
            op.execute(
                f"DROP INDEX CONCURRENTLY IF EXISTS {_COMPOSITE_INDEX}"
            )
    else:
        op.drop_index(_FAVOURITE_INDEX, table_name="media_items", if_exists=True)
        op.drop_index(_COMPOSITE_INDEX, table_name="media_items", if_exists=True)
