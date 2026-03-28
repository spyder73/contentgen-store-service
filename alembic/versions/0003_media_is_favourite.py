"""add is_favourite to media_items

Revision ID: 0003
Revises: 0002
Create Date: 2026-03-28
"""
from alembic import op
import sqlalchemy as sa

revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.add_column(
        'media_items',
        sa.Column('is_favourite', sa.Boolean(), nullable=False, server_default=sa.text('FALSE'))
    )

def downgrade() -> None:
    op.drop_column('media_items', 'is_favourite')
