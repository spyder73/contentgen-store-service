"""Migration 0055: scene_videos.pictures."""
from __future__ import annotations

import importlib.util
from pathlib import Path

from app.models import SceneVideo
from tests.test_chat import _render

PATH = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0055_scene_video_pictures.py"
_spec = importlib.util.spec_from_file_location("migration_0055", PATH)
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)


def test_0055_chains_off_0054():
    assert m.revision == "0055" and m.down_revision == "0054"


def test_upgrade_adds_a_nullable_json_column_and_downgrade_drops_it():
    up = _render(m.upgrade)
    assert "ALTER TABLE scene_videos ADD COLUMN pictures JSONB" in up and "NOT NULL" not in up
    assert "ALTER TABLE scene_videos DROP COLUMN pictures" in _render(m.downgrade)


def test_the_model_has_the_column():
    assert SceneVideo.__table__.columns["pictures"].nullable
