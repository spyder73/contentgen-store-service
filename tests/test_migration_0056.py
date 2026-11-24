"""Migration 0056: the list's stored facts, rendered offline in the postgres dialect."""
from __future__ import annotations

import importlib.util
from pathlib import Path

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


def test_upgrade_backfills_the_last_line_from_the_newest_kept_message():
    sql = _render(_module().upgrade)
    assert "SELECT DISTINCT ON (thread_id) thread_id, role, kind, text FROM chat_messages" in sql
    assert "kind IN ('text', 'approval', 'error', 'event', 'media')" in sql
    assert "ORDER BY thread_id, seq DESC" in sql
    assert "left(btrim(regexp_replace(m.text, '\\s+', ' ', 'g')), 140)" in sql


def test_upgrade_backfills_the_run_count_from_the_routine_event_lines():
    sql = _render(_module().upgrade)
    assert "UPDATE routines r SET run_count = c.runs" in sql
    assert "m.kind = 'event' AND left(m.text, 13) = 'Routine run: '" in sql
    assert "r.id::text = c.routine_id" in sql


def test_upgrade_backfills_the_thumbnail_from_the_newest_image_of_the_newest_production():
    sql = _render(_module().upgrade)
    assert "UPDATE chat_threads t SET thumb_media_id = x.media_id" in sql
    assert "SELECT DISTINCT ON (p.thread_id)" in sql and "ORDER BY p.thread_id, p.created_at DESC" in sql
    assert "mi.type = 'image'" in sql and "ORDER BY e.ord DESC LIMIT 1" in sql
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
