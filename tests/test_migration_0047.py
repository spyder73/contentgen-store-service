"""Migration 0047: saved GPT Image 2.5 Flare picks move to 2.6 Flare; upscale joins saved edit lists."""
from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text

PATH = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0047_gpt_image_2_6_and_upscale.py"
_spec = importlib.util.spec_from_file_location("migration_0047", PATH)
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)

OLD, NEW, UPSCALE = m.OLD, m.NEW, m.UPSCALE


def _img(model):
    return {"provider": "runware", "model": model}


def test_moves_the_locked_image_model():
    s = {"generation": {"image": {"provider": "runware", "model": OLD, "settings": {"width": 720}}}}
    out = m.moved_settings(s)
    assert out["generation"]["image"] == {"provider": "runware", "model": NEW, "settings": {"width": 720}}
    assert s["generation"]["image"]["model"] == OLD


def test_moves_each_allowed_image_model():
    s = {"agent_tools": {"image_models": [_img(OLD), _img("google:4@2")]}}
    assert m.moved_settings(s)["agent_tools"]["image_models"] == [_img(NEW), _img("google:4@2")]


def test_both_flares_collapse_to_one_entry():
    s = {"agent_tools": {"image_models": [_img(OLD), _img("google:4@2"), _img(NEW)]}}
    assert m.moved_settings(s)["agent_tools"]["image_models"] == [_img(NEW), _img("google:4@2")]


def test_leaves_everything_else():
    cases = [
        {"generation": {"image": _img("openai:gpt-image@2.5-sunburst")}},
        {"pipeline": {"note": OLD}},
        {"generation": {"image": {"provider": "runware", "model": "x", "settings": {"a": 1}}}},
    ]
    for s in cases:
        before = copy.deepcopy(s)
        assert m.moved_settings(s) is None
        assert s == before


def test_upscale_joins_a_saved_edit_list():
    four = [_img("a"), _img("b"), _img("c"), _img("d")]
    out = m.moved_settings({"agent_tools": {"video_edit_models": copy.deepcopy(four)}})
    assert out["agent_tools"]["video_edit_models"] == four + [UPSCALE]
    for lst in ([_img("a"), UPSCALE], [_img(str(i)) for i in range(5)], []):
        assert m.moved_settings({"agent_tools": {"video_edit_models": lst}}) is None
    assert m.moved_settings({"agent_tools": {}}) is None


def test_0047_chains_onto_0046():
    assert m.revision == "0047" and m.down_revision == "0046"


def test_upgrade_rewrites_rows_on_sqlite():
    eng = create_engine("sqlite://")
    rows = {
        "a": {"generation": {"image": _img(OLD)}},
        "b": {"agent_tools": {"image_models": [_img(OLD), _img(NEW)]}},
        "c": {"agent_tools": {"video_edit_models": [_img("a"), _img("b"), _img("c"), _img("d")]}},
        "d": {"theme": "dark"},
    }
    with eng.begin() as conn:
        conn.execute(text("CREATE TABLE user_settings (user_id TEXT PRIMARY KEY, settings JSON)"))
        for k, v in rows.items():
            conn.execute(text("INSERT INTO user_settings VALUES (:k, :v)"), {"k": k, "v": json.dumps(v)})
        untouched = conn.execute(text("SELECT settings FROM user_settings WHERE user_id='d'")).scalar()
        with Operations.context(MigrationContext.configure(conn)):
            m.upgrade()
        got = {
            k: v if isinstance(v, dict) else json.loads(v)
            for k, v in conn.execute(text("SELECT user_id, settings FROM user_settings"))
        }
        assert conn.execute(text("SELECT settings FROM user_settings WHERE user_id='d'")).scalar() == untouched
    assert got["a"]["generation"]["image"]["model"] == NEW
    assert got["b"]["agent_tools"]["image_models"] == [_img(NEW)]
    assert got["c"]["agent_tools"]["video_edit_models"][-1] == UPSCALE
    assert len(got["c"]["agent_tools"]["video_edit_models"]) == 5


def test_odd_shapes_do_not_raise():
    for s in ({"generation": "x"}, {"generation": [1]}, {"agent_tools": "x"}, {"agent_tools": [1]},
              {"generation": {"image": "x"}}, {"agent_tools": {"image_models": "x", "video_edit_models": "x"}}):
        assert m.moved_settings(s) is None
