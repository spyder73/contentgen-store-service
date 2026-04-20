"""add media file data storage columns

Revision ID: 0006
Revises: 0005
Create Date: 2026-04-08

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("media_items", sa.Column("file_data", sa.LargeBinary(), nullable=True))
    op.add_column("media_items", sa.Column("file_mime_type", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("media_items", "file_mime_type")
    op.drop_column("media_items", "file_data")
