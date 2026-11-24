"""chat list rows: a thread's last line and picture, a routine's run count

Revision ID: 0056
Revises: 0055
Create Date: 2026-11-24

``chat_threads`` keeps the last line the owner would read (role, kind, a one-line preview), written when a
message is appended, and the id of the newest picture a production of the chat made (no foreign key). A routine
keeps how many runs it finished. The list reads these instead of the messages. Existing rows are filled in
SQL: the last line is a whitespace-collapsed cut of 140 characters of the newest kept message (markdown in it
clears at the chat's next message), the run count counts the thread's "Routine run: " event lines, and the
picture is the newest image of the newest production.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0056"
down_revision: Union[str, None] = "0055"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_UUID = "'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'"

BACKFILL_LAST_LINE = r"""
UPDATE chat_threads t
SET last_role = m.role,
    last_kind = m.kind,
    last_text = left(btrim(regexp_replace(m.text, '\s+', ' ', 'g')), 140)
FROM (
    SELECT DISTINCT ON (thread_id) thread_id, role, kind, text
    FROM chat_messages
    WHERE kind IN ('text', 'approval', 'error', 'event', 'media') AND btrim(text) <> ''
    ORDER BY thread_id, seq DESC
) m
WHERE m.thread_id = t.id
"""

BACKFILL_RUN_COUNT = """
UPDATE routines r
SET run_count = c.runs
FROM (
    SELECT t.routine_id AS routine_id, count(*) AS runs
    FROM chat_messages m
    JOIN chat_threads t ON t.id = m.thread_id
    WHERE t.routine_id IS NOT NULL AND m.kind = 'event' AND left(m.text, 13) = 'Routine run: '
    GROUP BY t.routine_id
) c
WHERE r.id::text = c.routine_id
"""

BACKFILL_THUMB = f"""
UPDATE chat_threads t
SET thumb_media_id = x.media_id
FROM (
    SELECT DISTINCT ON (p.thread_id) p.thread_id AS thread_id, (
        SELECT mi.id::text
        FROM jsonb_array_elements_text(
            CASE WHEN jsonb_typeof(p.artifacts -> 'media_ids') = 'array' THEN p.artifacts -> 'media_ids' ELSE '[]'::jsonb END
        ) WITH ORDINALITY AS e(media_id, ord)
        JOIN media_items mi
          ON mi.id = (CASE WHEN e.media_id ~* {_UUID} THEN e.media_id::uuid END) AND mi.type = 'image'
        ORDER BY e.ord DESC
        LIMIT 1
    ) AS media_id
    FROM chat_productions p
    ORDER BY p.thread_id, p.created_at DESC
) x
WHERE x.thread_id = t.id AND x.media_id IS NOT NULL
"""


def upgrade() -> None:
    for name in ("last_text", "last_role", "last_kind"):
        op.add_column("chat_threads", sa.Column(name, sa.Text(), nullable=False, server_default=""))
    op.add_column("chat_threads", sa.Column("thumb_media_id", sa.Text(), nullable=True))
    op.add_column("routines", sa.Column("run_count", sa.Integer(), nullable=False, server_default="0"))
    op.create_index("ix_chat_productions_thread_closed", "chat_productions", ["thread_id", "closed_at"])
    op.execute(BACKFILL_LAST_LINE)
    op.execute(BACKFILL_RUN_COUNT)
    op.execute(BACKFILL_THUMB)


def downgrade() -> None:
    op.drop_index("ix_chat_productions_thread_closed", table_name="chat_productions")
    op.drop_column("routines", "run_count")
    for name in ("thumb_media_id", "last_kind", "last_role", "last_text"):
        op.drop_column("chat_threads", name)
