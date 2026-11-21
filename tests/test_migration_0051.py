"""Migration 0051: the four clip-style approval actions."""
from __future__ import annotations

import importlib.util
from pathlib import Path

PATH = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0051_clipstyle_chat_actions.py"
_spec = importlib.util.spec_from_file_location("migration_0051", PATH)
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)


def test_0051_adds_the_four_clipstyle_actions():
    assert m.revision == "0051" and m.down_revision == "0050"
    assert m._ACTIONS_0051.endswith("'clipstyle.build', 'clipstyle.edit', 'clipstyle.save', 'clipstyle.publish'")
    assert m._ACTIONS_0051.startswith(m._ACTIONS_0050)
