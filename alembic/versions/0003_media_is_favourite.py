"""add is_favourite to media_items

Revision ID: 0003
Revises: 0002
Create Date: 2026-03-28

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

def upgrade() -> None:
    op.add_column(
        'media_items',
        sa.Column('is_favourite', sa.Boolean(), nullable=False, server_default=sa.text('FALSE'))
    )

def downgrade() -> None:
    op.drop_column('media_items', 'is_favourite')
