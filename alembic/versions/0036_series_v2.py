"""series v2: template binding, cast fields, episode ledger

Revision ID: 0036
Revises: 0035
Create Date: 2026-09-13

Turns the three legacy series tables into the backbone of a running show:

  * ``series``     binds to the pipeline template it runs (``template_id``) and
    carries the durable show rules (``memories``), the per-checkpoint input
    wiring (``slot_map``) and a free parameter bag (``parameters``). The
    concept text stays in ``concept`` — there is no free-text bible.
  * ``characters`` becomes the cast sheet: each row is a ``kind`` of
    character/place/prop, carries identity ``anchors`` and may point at a voice
    sample in ``media_items``.
  * ``episodes``   becomes the ledger of what actually ran: ``status``,
    ``run_id``, ``idea_id``, ``clip_id``, the ``storyline`` blob and the
    closing frame (``last_frame_media_id``).

The old frontend stashed status/run_id/clip_id inside ``episodes.metadata``;
the backfill lifts those into the real columns and leaves ``metadata``
untouched, so anything still reading the blob keeps working.

``voice_snippets`` is dropped: nothing ever wrote it, and voice now lives on
the cast row as ``characters.voice_media_id``.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0036"
down_revision: Union[str, None] = "0035"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Exposed so the tests can execute the exact statements that ship, instead of
# asserting on a copy of them. ``->>`` yields NULL for an absent key on both
# postgres jsonb and sqlite JSON, so the guard needs no existence operator.
BACKFILL_STATEMENTS: tuple[str, ...] = tuple(
    f"UPDATE episodes SET {column} = metadata->>'{column}' "
    f"WHERE metadata->>'{column}' IS NOT NULL AND metadata->>'{column}' <> ''"
    for column in ("status", "run_id", "clip_id")
)


def upgrade() -> None:
    # ── series: what this show runs, and the rules it runs under ────────────
    op.add_column("series", sa.Column("template_id", sa.Text(), nullable=True))
    op.add_column(
        "series",
        sa.Column("memories", postgresql.JSONB(), nullable=False, server_default="[]"),
    )
    op.add_column(
        "series",
        sa.Column("slot_map", postgresql.JSONB(), nullable=False, server_default="{}"),
    )
    op.add_column(
        "series",
        sa.Column("parameters", postgresql.JSONB(), nullable=False, server_default="{}"),
    )

    # ── characters: the cast sheet ──────────────────────────────────────────
    op.add_column(
        "characters",
        sa.Column("kind", sa.Text(), nullable=False, server_default="character"),
    )
    op.add_column(
        "characters",
        sa.Column("anchors", postgresql.JSONB(), nullable=False, server_default="[]"),
    )
    op.add_column(
        "characters",
        sa.Column(
            "voice_media_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("media_items.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )

    # ── episodes: the ledger of what actually ran ───────────────────────────
    op.add_column(
        "episodes",
        sa.Column("status", sa.Text(), nullable=False, server_default="draft"),
    )
    op.add_column("episodes", sa.Column("run_id", sa.Text(), nullable=True))
    op.add_column("episodes", sa.Column("idea_id", sa.Text(), nullable=True))
    op.add_column("episodes", sa.Column("clip_id", sa.Text(), nullable=True))
    op.add_column(
        "episodes",
        sa.Column("storyline", postgresql.JSONB(), nullable=False, server_default="{}"),
    )
    op.add_column(
        "episodes",
        sa.Column(
            "last_frame_media_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("media_items.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )

    # Lift what the old frontend wrote into metadata; leave metadata as it is.
    for statement in BACKFILL_STATEMENTS:
        op.execute(sa.text(statement))

    # ── voice_snippets: dead table ──────────────────────────────────────────
    # Dropping the table takes its index with it (as 0002's own downgrade does).
    # Guarded: env.py runs the whole migration in one transaction, so a raise
    # here would roll back all thirteen ADD COLUMNs — and the startup runner
    # serves anyway, leaving an ORM that references columns the database does
    # not have. A table that is already gone is not worth that.
    if _voice_snippets_exists():
        op.drop_table("voice_snippets")


def _voice_snippets_exists() -> bool:
    """Whether the dead table is still there. An offline (``--sql``) render has
    no connection to inspect, so it emits the drop unconditionally — offline
    scripts are read by a human before they are run."""
    context = op.get_context()
    if context.as_sql:
        return True
    return sa.inspect(op.get_bind()).has_table("voice_snippets")


def downgrade() -> None:
    # The table comes back empty — it never held a row.
    op.create_table(
        "voice_snippets",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "character_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("characters.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("file_url", sa.Text(), server_default=""),
        sa.Column("duration", sa.Float(), server_default="0.0"),
        sa.Column("metadata", postgresql.JSONB(), server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_voice_snippets_character_id", "voice_snippets", ["character_id"])

    for column in ("last_frame_media_id", "storyline", "clip_id", "idea_id", "run_id", "status"):
        op.drop_column("episodes", column)
    for column in ("voice_media_id", "anchors", "kind"):
        op.drop_column("characters", column)
    for column in ("parameters", "slot_map", "memories", "template_id"):
        op.drop_column("series", column)
