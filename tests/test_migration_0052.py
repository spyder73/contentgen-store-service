"""Migration 0052: scene_videos and the scene.build / scene.render approval actions."""
from __future__ import annotations

import importlib.util
from pathlib import Path

from app.models import ChatApproval, SceneVideo
from tests.test_chat import _render

PATH = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0052_scene_videos.py"
_spec = importlib.util.spec_from_file_location("migration_0052", PATH)
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)


def test_0052_chains_and_extends_the_action_list():
    assert m.revision == "0052" and m.down_revision == "0051"
    assert m._ACTIONS_0052 == f"{m._ACTIONS_0051}, 'scene.build', 'scene.render'"


def test_upgrade_creates_the_table_and_widens_the_check():
    sql = _render(m.upgrade)
    assert "CREATE TABLE scene_videos" in sql
    assert "FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE" in sql
    assert "style_ref TEXT DEFAULT 'sceneStarter@1' NOT NULL" in sql
    assert "CREATE INDEX ix_scene_videos_user_created ON scene_videos (user_id, created_at)" in sql
    assert "CONSTRAINT ck_scene_videos_status CHECK (status IN ('draft', 'voiced', 'building', 'built', 'rendering', 'ready', 'failed'))" in sql
    assert f"ADD CONSTRAINT ck_chat_approvals_action CHECK (action IN ({m._ACTIONS_0052}))" in sql
    assert "'clipstyle.publish', 'scene.build', 'scene.render')" in sql


def test_downgrade_refuses_while_scene_approvals_exist_then_drops_the_table():
    sql = _render(m.downgrade)
    assert "chat_approvals WHERE action IN ('scene.build', 'scene.render')" in sql
    assert sql.count("RAISE EXCEPTION") == 1
    assert f"CHECK (action IN ({m._ACTIONS_0051}))" in sql
    assert sql.index("RAISE EXCEPTION") < sql.index("ADD CONSTRAINT") < sql.index("DROP TABLE scene_videos")


def test_the_models_match_the_migration():
    checks = {c.name: str(c.sqltext) for c in ChatApproval.__table__.constraints if getattr(c, "sqltext", None) is not None}
    assert checks["ck_chat_approvals_action"] == f"action IN ({m._ACTIONS_0052}, 'routine.budget', 'telegram.targets')"  # 0057 extended it
    video = {c.name: str(c.sqltext) for c in SceneVideo.__table__.constraints if getattr(c, "sqltext", None) is not None}
    assert "'draft', 'voiced', 'building', 'built', 'rendering', 'ready', 'failed'" in video["ck_scene_videos_status"]
    assert {c.name for c in SceneVideo.__table__.columns} == {
        "id", "user_id", "thread_id", "style_ref", "title", "status", "files", "files_hash", "last_report",
        "bundle", "narration_media_id", "voice_timeline", "voice_takes", "pictures", "plan", "final_media_id", "session_id", "woken_at",
        "created_at", "updated_at",
    }
