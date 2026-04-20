"""credits ledger + balance columns on users

Revision ID: 0010
Revises: 0009
Create Date: 2026-04-17

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # is_admin is ONLY settable via direct SQL — no API mutates it.
    op.add_column(
        "users",
        sa.Column("credits_balance", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.add_column(
        "users",
        sa.Column("credits_reserved", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.add_column(
        "users",
        sa.Column(
            "is_admin",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("FALSE"),
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "daily_spend_limit",
            sa.BigInteger(),
            nullable=False,
            server_default="5000",
        ),
    )

    op.create_table(
        "credits_ledger",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=False),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "user_id",
            sa.dialects.postgresql.UUID(as_uuid=False),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("delta", sa.BigInteger(), nullable=False),
        sa.Column(
            "pipeline_run_id",
            sa.dialects.postgresql.UUID(as_uuid=False),
            nullable=True,
        ),
        sa.Column("checkpoint_id", sa.Text(), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("provider", sa.Text(), nullable=True),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("cost_usd", sa.Numeric(12, 6), nullable=True),
        sa.Column("cost_source", sa.Text(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "admin_user_id",
            sa.dialects.postgresql.UUID(as_uuid=False),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("idempotency_key", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "kind IN ('grant','hold','release','debit','adjust')",
            name="credits_ledger_kind_valid",
        ),
        sa.CheckConstraint(
            "(kind <> 'grant') OR (admin_user_id IS NOT NULL)",
            name="credits_ledger_grant_admin_required",
        ),
        sa.CheckConstraint(
            "(kind <> 'hold') OR (delta > 0)",
            name="credits_ledger_hold_positive",
        ),
        sa.CheckConstraint(
            "(kind <> 'debit') OR (delta < 0)",
            name="credits_ledger_debit_negative",
        ),
        sa.CheckConstraint(
            "(kind <> 'grant') OR (delta > 0)",
            name="credits_ledger_grant_positive",
        ),
        sa.CheckConstraint(
            "(kind <> 'release') OR (delta > 0)",
            name="credits_ledger_release_positive",
        ),
    )
    op.create_index(
        "ix_credits_ledger_user_created",
        "credits_ledger",
        ["user_id", sa.text("created_at DESC")],
    )
    op.create_index(
        "ix_credits_ledger_pipeline_run",
        "credits_ledger",
        ["pipeline_run_id"],
        postgresql_where=sa.text("pipeline_run_id IS NOT NULL"),
    )
    op.create_index(
        "uq_credits_ledger_idempotency",
        "credits_ledger",
        ["idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )

    # Append-only enforcement via trigger (store service is DB owner, so GRANT/
    # REVOKE alone wouldn't bind it).
    op.execute(
        """
        CREATE OR REPLACE FUNCTION credits_ledger_immutable()
        RETURNS TRIGGER LANGUAGE plpgsql AS $$
        BEGIN
          RAISE EXCEPTION 'credits_ledger is append-only';
        END; $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER credits_ledger_no_update
          BEFORE UPDATE ON credits_ledger
          FOR EACH ROW EXECUTE FUNCTION credits_ledger_immutable();
        """
    )
    op.execute(
        """
        CREATE TRIGGER credits_ledger_no_delete
          BEFORE DELETE ON credits_ledger
          FOR EACH ROW EXECUTE FUNCTION credits_ledger_immutable();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS credits_ledger_no_delete ON credits_ledger")
    op.execute("DROP TRIGGER IF EXISTS credits_ledger_no_update ON credits_ledger")
    op.execute("DROP FUNCTION IF EXISTS credits_ledger_immutable()")
    op.drop_index("uq_credits_ledger_idempotency", table_name="credits_ledger")
    op.drop_index("ix_credits_ledger_pipeline_run", table_name="credits_ledger")
    op.drop_index("ix_credits_ledger_user_created", table_name="credits_ledger")
    op.drop_table("credits_ledger")
    op.drop_column("users", "daily_spend_limit")
    op.drop_column("users", "is_admin")
    op.drop_column("users", "credits_reserved")
    op.drop_column("users", "credits_balance")
