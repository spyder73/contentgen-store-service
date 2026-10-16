"""clipstyle design toolkit amendment A1 (named icon assets): widen the asset label CHECK

Revision ID: 0040
Revises: 0039
Create Date: 2026-10-16

Relaxes `clipstyle_assets_label_valid` (migration 0039) from the five fixed single-slot labels to
also admit `icon-<name>` (name [a-z0-9]{1,24}) (PF5-1): the Python-side check in
`app/stores/clipstyle_packages.check_package_config` already accepts it, and the Postgres CHECK must
not reject it at `freeze_assets` insert time. The downgrade is refused while any `icon-` asset rows
exist, since reverting the CHECK would make those frozen rows retroactively invalid (icons are
frozen/append-only like every other brand asset).
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0040"
down_revision: Union[str, None] = "0039"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint("clipstyle_assets_label_valid", "clipstyle_assets", type_="check")
    op.create_check_constraint(
        "clipstyle_assets_label_valid", "clipstyle_assets",
        "label ~ '^(logo|wordmark|texture|background|icon|icon-[a-z0-9]{1,24})$'",
    )


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM clipstyle_assets WHERE label LIKE 'icon-%') THEN "
        "RAISE EXCEPTION 'downgrade 0040 refused: named icon assets are frozen'; END IF; END $$"
    )
    op.drop_constraint("clipstyle_assets_label_valid", "clipstyle_assets", type_="check")
    op.create_check_constraint(
        "clipstyle_assets_label_valid", "clipstyle_assets",
        "label IN ('logo','wordmark','texture','background','icon')",
    )
