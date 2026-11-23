"""Migration 0053: scene_videos.voice_takes."""
from __future__ import annotations

import importlib.util
from pathlib import Path

from app.models import SceneVideo
from tests.test_chat import _render

PATH = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0053_scene_video_voice_takes.py"
_spec = importlib.util.spec_from_file_location("migration_0053", PATH)
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)


def test_0053_chains_off_0052():
    assert m.revision == "0053" and m.down_revision == "0052"


def test_upgrade_adds_a_nullable_json_column_and_downgrade_drops_it():
    up = _render(m.upgrade)
    assert "ALTER TABLE scene_videos ADD COLUMN voice_takes JSONB" in up and "NOT NULL" not in up
    assert "ALTER TABLE scene_videos DROP COLUMN voice_takes" in _render(m.downgrade)


def test_the_model_has_the_column():
    col = SceneVideo.__table__.columns["voice_takes"]
    assert col.nullable
