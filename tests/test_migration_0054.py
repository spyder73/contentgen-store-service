"""Migration 0054: scene_style_examples."""
from __future__ import annotations

import importlib.util
from pathlib import Path

from app.models import SceneStyleExample
from tests.test_chat import _render

PATH = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0054_scene_style_examples.py"
_spec = importlib.util.spec_from_file_location("migration_0054", PATH)
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)


def test_0054_chains_off_0053():
    assert m.revision == "0054" and m.down_revision == "0053"


def test_upgrade_creates_the_table_and_downgrade_drops_it():
    up = _render(m.upgrade)
    assert "CREATE TABLE scene_style_examples" in up
    assert "REFERENCES scene_videos (id) ON DELETE CASCADE" in up
    assert "UNIQUE (video_id)" in up or "video_id UUID NOT NULL UNIQUE" in up
    assert "plan JSONB NOT NULL" in up
    assert "CREATE INDEX ix_scene_style_examples_user_style_updated ON scene_style_examples (user_id, style_id, updated_at)" in up
    assert "DROP TABLE scene_style_examples" in _render(m.downgrade)


def test_the_model_matches():
    cols = SceneStyleExample.__table__.columns
    assert cols["video_id"].unique and not cols["plan"].nullable
    assert {c.name for c in cols} == {
        "id", "user_id", "style_id", "style_ref", "video_id", "title", "topic", "plan", "created_at", "updated_at",
    }
