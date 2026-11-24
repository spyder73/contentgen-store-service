"""chat list rows: a thread's last line and picture, a routine's run count

Revision ID: 0056
Revises: 0055
Create Date: 2026-11-24

``chat_threads`` keeps the last line the owner would read (role, kind, a one-line preview), written when a
message is appended, and the id of the newest picture a production of the chat made (no foreign key). A routine
keeps how many runs it finished. The list reads these instead of the messages. Existing rows are filled in
SQL: the last line is the newest message whose preview is not empty, by ``chat_preview.preview_line``'s rule
written as Postgres regular expressions (no code block, ref or markdown mark, a word cut at 140 characters;
a thread with no such message keeps no line), the run count counts the thread's "Routine run: " event lines,
and the picture is the newest image of the newest production that the thread's owner owns.
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

# ``chat_preview.preview_line`` step by step, as Postgres regexp_replace calls: (pattern, replacement, flags).
# Standard-conforming strings: a backslash in a literal is the regular expression's own.
PREVIEW_STEPS: tuple[tuple[str, str, str], ...] = (
    (r"```.*?(```|$)", " ", "g"),  # code blocks; an unclosed one runs to the end
    (r"!\[([^]]*)\]\([^)]*\)", r"\1", "g"),  # an image: its alt text
    (r"\[([^]]*)\]\([^)]*\)", r"\1", "g"),  # a link: its text
    (r"\m(media|clip):[\w-]+", " ", "g"),  # a bare media: or clip: ref
    (r"^[ \t]{0,3}((#{1,6}|>|[-*+]|[0-9]+[.)])[ \t]+|>)+", "", "gn"),  # heading, quote and list marks, per line
    (r"\*\*|__|~~|`", "", "g"),  # emphasis and code marks
    (r"\s+", " ", "g"),  # whitespace, collapsed
)


def _flat(column: str) -> str:
    """The SQL for ``column``'s first 4,000 characters as one plain, trimmed line (preview_line before its cut)."""
    sql = f"left({column}, 4000)"
    for pattern, replacement, flags in PREVIEW_STEPS:
        sql = f"regexp_replace({sql}, '{pattern}', '{replacement}', '{flags}')"
    return f"btrim({sql})"


# preview_line's cut: at most 140 characters, else 139 cut back to the last space (unless the 140th is one) and "…".
_CUT = """CASE WHEN char_length(f.flat) <= 140 THEN f.flat
    ELSE rtrim(CASE WHEN substr(f.flat, 140, 1) <> ' ' AND strpos(left(f.flat, 139), ' ') > 0
        THEN left(f.flat, 139 - strpos(reverse(left(f.flat, 139)), ' '))
        ELSE left(f.flat, 139) END) || '…'
    END"""

# Per thread, its newest message of a kind that has a line and whose line is not empty, as the write path keeps
# the previous line for a message with none: one backward walk of ux_chat_messages_thread_seq per thread.
BACKFILL_LAST_LINE = f"""
WITH lines AS (
    SELECT t.id AS thread_id, m.role, m.kind, m.line
    FROM chat_threads t
    CROSS JOIN LATERAL (
        SELECT c.role, c.kind, {_CUT} AS line
        FROM chat_messages c
        CROSS JOIN LATERAL (SELECT {_flat("c.text")} AS flat) f
        WHERE c.thread_id = t.id AND c.kind IN ('text', 'approval', 'error', 'event', 'media') AND f.flat <> ''
        ORDER BY c.seq DESC
        LIMIT 1
    ) m
)
UPDATE chat_threads t
SET last_role = lines.role, last_kind = lines.kind, last_text = lines.line
FROM lines
WHERE lines.thread_id = t.id
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
         AND mi.user_id = p.user_id
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
