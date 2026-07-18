"""CHECK constraints enforcing valid visibility values

Revision ID: 0029
Revises: 0028
Create Date: 2026-07-17

pipeline_templates.visibility and prompt_templates.visibility have always been
free-text columns validated only in application code (VALID_VISIBILITY sets in
app/stores/pipelines.py and app/stores/prompts.py). That leaves the door open
to bad rows from direct SQL, a future code path that forgets to validate, or a
partial migration. This adds a DB-level CHECK so a bad value can never land in
either table, regardless of how the row was written.

Existing rows are normalized to 'private' first so the ADD CONSTRAINT (which
validates all existing rows by default) cannot fail on legacy data. Both
statements run inside the same transaction as the rest of this migration
(ordinary, non-CONCURRENTLY DDL), so if anything here fails the whole
migration rolls back cleanly and the next boot's `alembic upgrade head` simply
retries it from scratch -- no partial-application state is possible.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0029"
down_revision: Union[str, None] = "0028"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_VALID_VISIBILITY = ("private", "assigned", "global")


def upgrade() -> None:
    conn = op.get_bind()
    for table in ("pipeline_templates", "prompt_templates"):
        conn.execute(
            sa.text(
                f"UPDATE {table} SET visibility = 'private' "
                "WHERE visibility IS NULL OR visibility NOT IN ('private','assigned','global')"
            )
        )

    op.create_check_constraint(
        "pipeline_templates_visibility_valid",
        "pipeline_templates",
        "visibility IN ('private','assigned','global')",
    )
    op.create_check_constraint(
        "prompt_templates_visibility_valid",
        "prompt_templates",
        "visibility IN ('private','assigned','global')",
    )


def downgrade() -> None:
    op.drop_constraint("prompt_templates_visibility_valid", "prompt_templates", type_="check")
    op.drop_constraint("pipeline_templates_visibility_valid", "pipeline_templates", type_="check")
