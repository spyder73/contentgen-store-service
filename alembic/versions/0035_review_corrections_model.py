"""review_corrections: model_id for the this_model scope

Revision ID: 0035
Revises: 0034
Create Date: 2026-09-10

Adds ``model_id`` to ``review_corrections``: the generator model a lesson is
about, carried by the new scope ``this_model`` ("this rule is about how one
model behaves"), which the application layer renders to any judge whose
checkpoint uses that model, in any pipeline of the user. ``scope`` has no
DB-level enum or check constraint (0033 constrains ``label`` only), so
widening it to this_checkpoint | this_pipeline | all_pipelines | this_model
needs no schema change here. The column defaults to '' so pre-existing rows
and older clients that never send it keep working.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0035"
down_revision: Union[str, None] = "0034"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "review_corrections",
        sa.Column("model_id", sa.Text(), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("review_corrections", "model_id")
