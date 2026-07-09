"""dataset template model_target

Revision ID: 0021
Revises: 0020
Create Date: 2026-07-09

Adds a `model_target` column to `dataset_templates` so a dataset can carry an
SDXL-vs-Z-Image training-target choice (values `'sdxl'` | `'z-image'`).

The column defaults to `'sdxl'` (server_default), so every existing row —
including migration 0019's default `'Identity Collage (16-tile)'` row — is
backfilled to `'sdxl'`. The `'Identity Collage (16-tile) — Z-Image'` row
seeded by migration 0020 is then updated to `'z-image'` since that preset is
tuned for Z-Image / ZIT training.

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0021"
down_revision: Union[str, None] = "0020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "dataset_templates",
        sa.Column("model_target", sa.Text(), nullable=False, server_default="sdxl"),
    )
    # The SDXL default row already gets 'sdxl' from the server_default; only the
    # seeded Z-Image preset needs to be flipped to 'z-image'.
    op.execute(
        """
        UPDATE dataset_templates
        SET model_target = 'z-image'
        WHERE name = 'Identity Collage (16-tile) — Z-Image' AND user_id IS NULL
        """
    )


def downgrade() -> None:
    op.drop_column("dataset_templates", "model_target")
