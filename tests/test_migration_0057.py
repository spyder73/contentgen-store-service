"""Migration 0057: routine budget end and ticks, press choices, chat Telegram targets, Telegram recipients."""
from __future__ import annotations

import importlib.util
from pathlib import Path

from app.models import ChatApproval, ChatProduction, ChatThread, TelegramRecipient
from tests.test_chat import _render

PATH = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0057_routine_autonomy.py"
_spec = importlib.util.spec_from_file_location("migration_0057", PATH)
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)


def test_0057_chains_off_0056():
    assert m.revision == "0057" and m.down_revision == "0056"


def test_upgrade_adds_the_columns_the_table_and_the_actions():
    up = _render(m.upgrade)
    for part in (
        "ADD COLUMN ends_at", "ADD COLUMN allowed JSONB", "ADD COLUMN choices JSONB",
        "ADD COLUMN telegram_targets JSONB", "CREATE TABLE telegram_recipients",
        "uq_telegram_recipients_user_tid", "'routine.budget'", "'telegram.targets'",
    ):
        assert part in up, part


def test_downgrade_refuses_while_new_cards_exist_then_drops():
    down = _render(m.downgrade)
    guard = down[down.index("DO $$"):]
    assert "'routine.budget'" in guard and "'telegram.targets'" in guard and "RAISE EXCEPTION" in guard
    assert "DROP TABLE telegram_recipients" in down
    for col in ("ends_at", "allowed"):
        assert f"ALTER TABLE chat_productions DROP COLUMN {col}" in down
    assert "ALTER TABLE chat_approvals DROP COLUMN choices" in down
    assert "ALTER TABLE chat_threads DROP COLUMN telegram_targets" in down


def test_the_models_mirror_it():
    p = ChatProduction.__table__.columns
    assert p["ends_at"].nullable and p["allowed"].nullable
    assert ChatApproval.__table__.columns["choices"].nullable
    assert not ChatThread.__table__.columns["telegram_targets"].nullable
    uniques = [tuple(c.name for c in u.columns) for u in TelegramRecipient.__table__.constraints
               if u.__class__.__name__ == "UniqueConstraint"]
    assert ("user_id", "telegram_id") in uniques
