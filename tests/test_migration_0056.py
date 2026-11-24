"""Migration 0056: the list's stored facts, rendered offline in the postgres dialect."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import text

from app.models import ChatProduction, ChatThread, Routine
from tests.test_chat import _render

PATH = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0056_chat_list_rows.py"


def _module():
    spec = importlib.util.spec_from_file_location("migration_0056_chat_list_rows", PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_it_chains_onto_0055():
    module = _module()
    assert (module.revision, module.down_revision) == ("0056", "0055")


def test_upgrade_adds_the_columns_and_the_index():
    sql = _render(_module().upgrade)
    for column in ("last_text", "last_role", "last_kind"):
        assert f"ALTER TABLE chat_threads ADD COLUMN {column} TEXT DEFAULT '' NOT NULL" in sql
    assert "ALTER TABLE chat_threads ADD COLUMN thumb_media_id TEXT" in sql
    assert "thumb_media_id TEXT NOT NULL" not in sql and "REFERENCES" not in sql
    assert "ALTER TABLE routines ADD COLUMN run_count INTEGER DEFAULT '0' NOT NULL" in sql
    assert "CREATE INDEX ix_chat_productions_thread_closed ON chat_productions (thread_id, closed_at)" in sql


def test_upgrade_backfills_the_last_line_from_the_newest_message_with_a_line():
    sql = _render(_module().upgrade)
    # Per thread, a backward walk to its newest message of a kind that has a line, skipping one whose line is empty.
    assert "WITH lines AS ( SELECT t.id AS thread_id, m.role, m.kind, m.line FROM chat_threads t CROSS JOIN LATERAL (" in sql
    assert "WHERE c.thread_id = t.id AND c.kind IN ('text', 'approval', 'error', 'event', 'media') AND f.flat <> ''" in sql
    assert "ORDER BY c.seq DESC LIMIT 1" in sql
    assert "SET last_role = lines.role, last_kind = lines.kind, last_text = lines.line FROM lines WHERE lines.thread_id = t.id" in sql


def test_the_last_line_is_preview_lines_rule_step_by_step():
    module = _module()
    sql = _render(module.upgrade)
    # The steps nest in preview_line's order over the first 4,000 characters, trimmed at the end.
    nested = "btrim(" + "regexp_replace(" * len(module.PREVIEW_STEPS) + "left(c.text, 4000), "
    assert nested in sql
    patterns = [pattern for pattern, _, _ in module.PREVIEW_STEPS]
    assert patterns[0].startswith("```") and patterns[-1] == r"\s+"
    for pattern, replacement, flags in module.PREVIEW_STEPS:
        assert f"'{pattern}', '{replacement}', '{flags}')" in sql
    # Code blocks go first, so no fence, ref or mark is ever written; only the line marks match per line.
    assert [flags for _, _, flags in module.PREVIEW_STEPS].count("gn") == 1
    assert "CASE WHEN char_length(f.flat) <= 140 THEN f.flat" in sql and "|| '…'" in sql


@pytest.mark.parametrize("name", ["BACKFILL_LAST_LINE", "BACKFILL_RUN_COUNT", "BACKFILL_THUMB"])
def test_no_backfill_reads_as_a_bind_parameter(name):
    # op.execute wraps a string in text(): a ":name" in a pattern would become a bind parameter with no value.
    assert text(getattr(_module(), name))._bindparams == {}


def test_upgrade_backfills_the_run_count_from_the_routine_event_lines():
    sql = _render(_module().upgrade)
    assert "UPDATE routines r SET run_count = c.runs" in sql
    assert "m.kind = 'event' AND left(m.text, 13) = 'Routine run: '" in sql
    assert "r.id::text = c.routine_id" in sql


def test_upgrade_backfills_the_thumbnail_from_the_owners_newest_image_of_the_newest_production():
    sql = _render(_module().upgrade)
    assert "UPDATE chat_threads t SET thumb_media_id = x.media_id" in sql
    assert "SELECT DISTINCT ON (p.thread_id)" in sql and "ORDER BY p.thread_id, p.created_at DESC" in sql
    assert "mi.type = 'image'" in sql and "ORDER BY e.ord DESC LIMIT 1" in sql
    # Only a picture the thread's owner owns, as the write path takes it.
    assert "AND mi.type = 'image' AND mi.user_id = p.user_id" in sql
    # Only well-formed uuids are cast: the cast sits behind the regex test.
    assert "CASE WHEN e.media_id ~* '^[0-9a-f]{8}-" in sql and "THEN e.media_id::uuid END" in sql


def test_downgrade_drops_all_of_it():
    sql = _render(_module().downgrade)
    assert "DROP INDEX ix_chat_productions_thread_closed" in sql
    assert "ALTER TABLE routines DROP COLUMN run_count" in sql
    for column in ("last_text", "last_role", "last_kind", "thumb_media_id"):
        assert f"ALTER TABLE chat_threads DROP COLUMN {column}" in sql


def test_the_models_mirror_the_columns_and_the_index():
    threads = ChatThread.__table__.c
    for name in ("last_text", "last_role", "last_kind"):
        assert (threads[name].nullable, threads[name].server_default.arg) == (False, "")
    assert threads.thumb_media_id.nullable and not threads.thumb_media_id.foreign_keys
    run_count = Routine.__table__.c.run_count
    assert (run_count.nullable, run_count.server_default.arg) == (False, "0")
    index = next(i for i in ChatProduction.__table__.indexes if i.name == "ix_chat_productions_thread_closed")
    assert [c.name for c in index.columns] == ["thread_id", "closed_at"]
