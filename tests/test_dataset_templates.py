"""Tests for the dataset_templates prompt-quality migration (0020).

Migration 0019 seeded a system default `dataset_templates` row whose
`collage_prompt` contained Handlebars templating that the Go backend does
NOT render (it is sent literally to the image model), plus instructions
that produced low-variety training data (constant neutral background and
expression). Migration 0020 fixes the prompt text and adds a second
system template tuned for Z-Image (ZIT) training.

These tests load the migration module directly by file path (it lives in
a numerically-prefixed file that isn't a valid Python package name) and
assert on the prompt content itself, without touching a real database.
"""
from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.schemas import (
    CollageStage,
    DatasetTemplateCreate,
    DatasetTemplateOut,
    DatasetTemplateUpdate,
)
from app.stores import dataset_templates

VERSIONS_DIR = Path(__file__).resolve().parent.parent / "alembic" / "versions"
MIGRATION_PATH = VERSIONS_DIR / "0020_dataset_templates_prompt_quality.py"
MIGRATION_0021_PATH = VERSIONS_DIR / "0021_dataset_template_model_target.py"
MIGRATION_0022_PATH = VERSIONS_DIR / "0022_dataset_template_collage_stages.py"
MIGRATION_0023_PATH = VERSIONS_DIR / "0023_dataset_template_balanced_prompts.py"
MIGRATION_0024_PATH = VERSIONS_DIR / "0024_dataset_template_fullbody_diversity.py"
MIGRATION_0025_PATH = VERSIONS_DIR / "0025_dataset_template_lean_prompts.py"
MIGRATION_0026_PATH = VERSIONS_DIR / "0026_dataset_template_fullbody_singles.py"


def _load_migration_module():
    spec = importlib.util.spec_from_file_location(
        "migration_0020_dataset_templates_prompt_quality", MIGRATION_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_migration_0021_module():
    spec = importlib.util.spec_from_file_location(
        "migration_0021_dataset_template_model_target", MIGRATION_0021_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_migration_0022_module():
    spec = importlib.util.spec_from_file_location(
        "migration_0022_dataset_template_collage_stages", MIGRATION_0022_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_migration_0023_module():
    spec = importlib.util.spec_from_file_location(
        "migration_0023_dataset_template_balanced_prompts", MIGRATION_0023_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_migration_0024_module():
    spec = importlib.util.spec_from_file_location(
        "migration_0024_dataset_template_fullbody_diversity", MIGRATION_0024_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_migration_0025_module():
    spec = importlib.util.spec_from_file_location(
        "migration_0025_dataset_template_lean_prompts", MIGRATION_0025_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_migration_0026_module():
    spec = importlib.util.spec_from_file_location(
        "migration_0026_dataset_template_fullbody_singles", MIGRATION_0026_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_module_imports():
    module = _load_migration_module()
    assert module.revision == "0020"
    assert module.down_revision == "0019"
    assert module.branch_labels is None
    assert module.depends_on is None
    assert hasattr(module, "upgrade")
    assert hasattr(module, "downgrade")


def test_improved_prompt_has_no_handlebars():
    module = _load_migration_module()
    prompt = module.IMPROVED_COLLAGE_PROMPT
    assert "{{#" not in prompt
    assert "{{@" not in prompt


def test_improved_prompt_has_variety_markers():
    module = _load_migration_module()
    prompt = module.IMPROVED_COLLAGE_PROMPT
    assert "DIFFERENT background" in prompt
    assert "FULL-BODY" in prompt
    assert "seamless 4x4 grid" in prompt


# ── migration 0021: model_target column + backfill ────────────────────────


def test_migration_0021_module_imports():
    module = _load_migration_0021_module()
    assert module.revision == "0021"
    assert module.down_revision == "0020"
    assert module.branch_labels is None
    assert module.depends_on is None
    assert hasattr(module, "upgrade")
    assert hasattr(module, "downgrade")


def test_migration_0021_adds_column_and_backfills():
    """The migration must add a NOT NULL model_target column defaulting to
    'sdxl' and flip the seeded Z-Image preset row to 'z-image'."""
    src = MIGRATION_0021_PATH.read_text()
    assert 'add_column' in src
    assert '"model_target"' in src
    assert "server_default=\"sdxl\"" in src
    assert "nullable=False" in src
    # backfill only the Z-Image preset (SDXL rows keep the server_default)
    assert "model_target = 'z-image'" in src
    assert "Identity Collage (16-tile) — Z-Image" in src
    # downgrade drops the column
    assert 'drop_column("dataset_templates", "model_target")' in src


# ── migration 0022: collage_stages + seed_reference_media_id + recipe seed ──


def test_migration_0022_module_imports():
    module = _load_migration_0022_module()
    assert module.revision == "0022"
    assert module.down_revision == "0021"
    assert module.branch_labels is None
    assert module.depends_on is None
    assert hasattr(module, "upgrade")
    assert hasattr(module, "downgrade")


def test_migration_0022_seeds_four_stage_recipe():
    module = _load_migration_0022_module()
    stages = module.COLLAGE_STAGES
    assert len(stages) == 4
    # the recipe must be JSON-serialisable (it is written as JSONB)
    json.dumps(stages)  # must not raise
    for st in stages:
        for key in (
            "label", "prompt", "width", "height",
            "grid_x", "grid_y", "inset_pct", "reference_policy",
        ):
            assert key in st, f"missing {key}"
        assert st["inset_pct"] == 0.015
    # stage geometry from Part A.1
    assert (stages[0]["width"], stages[0]["height"]) == (2048, 2048)
    assert (stages[0]["grid_x"], stages[0]["grid_y"]) == (4, 4)
    assert stages[0]["reference_policy"] == "identity_seed"
    # Stage 4 = portrait 4K, 3 columns x 2 rows, chained off collage 1
    assert (stages[3]["width"], stages[3]["height"]) == (2160, 3840)
    assert (stages[3]["grid_x"], stages[3]["grid_y"]) == (3, 2)
    assert stages[3]["reference_policy"] == "collage_1"


def test_migration_0022_adds_columns_and_seeds_both_system_rows():
    src = MIGRATION_0022_PATH.read_text()
    assert "add_column" in src
    assert '"collage_stages"' in src
    assert '"seed_reference_media_id"' in src
    # seeds BOTH seeded system rows (sdxl + z-image) via user_id IS NULL
    assert "WHERE user_id IS NULL" in src
    # downgrade drops both columns
    assert 'drop_column("dataset_templates", "seed_reference_media_id")' in src
    assert 'drop_column("dataset_templates", "collage_stages")' in src


# ── migration 0024: full-body outfit/activity diversity (2 stages replace 1) ──


def test_migration_0024_module_imports():
    module = _load_migration_0024_module()
    assert module.revision == "0024"
    assert module.down_revision == "0023"
    assert module.branch_labels is None
    assert module.depends_on is None
    assert hasattr(module, "upgrade")
    assert hasattr(module, "downgrade")


def test_migration_0024_stages_1_to_3_are_byte_identical_to_0023():
    """User decision: stages 1-3 (face rotation, expressions, angles/upper-body/
    wardrobe) are already good — 0024 must not touch their prompt text."""
    prev = _load_migration_0023_module()
    curr = _load_migration_0024_module()
    assert curr.COLLAGE_STAGES[0:3] == prev.COLLAGE_STAGES[0:3]


def test_migration_0024_has_five_stages():
    module = _load_migration_0024_module()
    stages = module.COLLAGE_STAGES
    assert len(stages) == 5
    json.dumps(stages)  # must be JSON-serialisable (written as JSONB)
    for st in stages:
        for key in (
            "label", "prompt", "width", "height",
            "grid_x", "grid_y", "inset_pct", "reference_policy",
        ):
            assert key in st, f"missing {key}"


def test_migration_0024_fullbody_stages_keep_portrait_geometry():
    """Stages 4 and 5 must keep the 0023 stage-4 portrait geometry: 2160x3840,
    3 columns x 2 rows (6 tiles), inset 0.015, chained off collage 1."""
    module = _load_migration_0024_module()
    for stage in module.COLLAGE_STAGES[3:5]:
        assert (stage["width"], stage["height"]) == (2160, 3840)
        assert (stage["grid_x"], stage["grid_y"]) == (3, 2)
        assert stage["grid_x"] * stage["grid_y"] == 6
        assert stage["inset_pct"] == 0.015
        assert stage["reference_policy"] == "collage_1"


def test_migration_0024_stage4_covers_outfit_style_keywords():
    module = _load_migration_0024_module()
    stage4 = module.COLLAGE_STAGES[3]
    assert "Outfit" in stage4["label"]
    keywords = [
        "classy formal",
        "business casual",
        "comfy casual",
        "streetwear",
        "sporty athleisure",
        "summer casual",
    ]
    hits = [kw for kw in keywords if kw in stage4["prompt"]]
    assert len(hits) >= 4, f"only matched {hits}"
    assert "DIFFERENT outfit STYLE" in stage4["prompt"]


def test_migration_0024_stage5_covers_activity_keywords():
    module = _load_migration_0024_module()
    stage5 = module.COLLAGE_STAGES[4]
    assert stage5["label"] == "Full-body Activities (portrait)"
    keywords = [
        "walking mid-stride",
        "jogging",
        "yoga",
        "cafe",
        "shop or market",
        "tending plants",
    ]
    hits = [kw for kw in keywords if kw in stage5["prompt"]]
    assert len(hits) >= 4, f"only matched {hits}"
    assert "DIFFERENT activity" in stage5["prompt"]
    # ≥2 tiles left / ≥2 tiles right + varied camera height mandate
    assert "at least 2" in stage5["prompt"]
    assert "camera height" in stage5["prompt"]


def test_migration_0024_upgrade_matches_0022_0023_precedent():
    src = MIGRATION_0024_PATH.read_text()
    assert "WHERE user_id IS NULL" in src
    assert "CAST(:stages AS jsonb)" in src


def test_migration_0024_downgrade_is_noop():
    module = _load_migration_0024_module()
    # must not raise and must not touch the database (no op.execute call)
    assert module.downgrade() is None


# ── migration 0025: lean prompts, positional mirrored-pair orientation ──────


def test_migration_0025_module_imports():
    module = _load_migration_0025_module()
    assert module.revision == "0025"
    assert module.down_revision == "0024"
    assert module.branch_labels is None
    assert module.depends_on is None
    assert hasattr(module, "upgrade")
    assert hasattr(module, "downgrade")


def test_migration_0025_has_five_stages_matching_0024_geometry():
    """Labels and geometry (size/grid/inset/reference_policy) must be
    identical to 0024 — only prompt text changes."""
    prev = _load_migration_0024_module()
    curr = _load_migration_0025_module()
    prev_stages = prev.COLLAGE_STAGES
    curr_stages = curr.COLLAGE_STAGES
    assert len(curr_stages) == 5
    json.dumps(curr_stages)  # must be JSON-serialisable (written as JSONB)
    assert len(curr_stages) == len(prev_stages)
    for prev_st, curr_st in zip(prev_stages, curr_stages):
        assert curr_st["label"] == prev_st["label"]
        assert curr_st["width"] == prev_st["width"]
        assert curr_st["height"] == prev_st["height"]
        assert curr_st["grid_x"] == prev_st["grid_x"]
        assert curr_st["grid_y"] == prev_st["grid_y"]
        assert curr_st["inset_pct"] == prev_st["inset_pct"]
        assert curr_st["reference_policy"] == prev_st["reference_policy"]
        # prompt text itself must actually have changed
        assert curr_st["prompt"] != prev_st["prompt"]


def test_migration_0025_prompts_drop_pipeline_meta_language():
    module = _load_migration_0025_module()
    for stage in module.COLLAGE_STAGES:
        prompt = stage["prompt"]
        assert "PRIMARY" not in prompt
        assert "SECONDARY" not in prompt
        assert "PRIORITY ORDER" not in prompt
        assert "previous version" not in prompt


def test_migration_0025_prompts_use_positional_mirrored_pairs():
    module = _load_migration_0025_module()
    for stage in module.COLLAGE_STAGES:
        prompt = stage["prompt"]
        assert "mirrored pairs" in prompt, f"missing in {stage['label']}"
        assert "RIGHT" in prompt, f"missing in {stage['label']}"


def test_migration_0025_prompts_have_grid_slicing_language():
    module = _load_migration_0025_module()
    for stage in module.COLLAGE_STAGES:
        assert "sliced into" in stage["prompt"], f"missing in {stage['label']}"


def test_migration_0025_stage4_covers_outfit_style_keywords():
    module = _load_migration_0025_module()
    stage4 = module.COLLAGE_STAGES[3]
    assert "Outfit" in stage4["label"]
    keywords = [
        "classy formal",
        "business casual",
        "comfy casual",
        "streetwear",
        "sporty athleisure",
        "summer casual",
    ]
    hits = [kw for kw in keywords if kw in stage4["prompt"]]
    assert len(hits) >= 4, f"only matched {hits}"


def test_migration_0025_stage5_covers_activity_keywords():
    module = _load_migration_0025_module()
    stage5 = module.COLLAGE_STAGES[4]
    assert stage5["label"] == "Full-body Activities (portrait)"
    keywords = [
        "walking mid-stride",
        "jogging",
        "yoga",
        "cafe",
        "shop or market",
        "tending plants",
    ]
    hits = [kw for kw in keywords if kw in stage5["prompt"]]
    assert len(hits) >= 4, f"only matched {hits}"


def test_migration_0025_upgrade_matches_precedent():
    src = MIGRATION_0025_PATH.read_text()
    assert "WHERE user_id IS NULL" in src
    assert "CAST(:stages AS jsonb)" in src


def test_migration_0025_downgrade_is_noop():
    module = _load_migration_0025_module()
    # must not raise and must not touch the database (no op.execute call)
    assert module.downgrade() is None


# ── migration 0026: Identity Collages + Full-body Singles (Z-Image) preset ──


def test_migration_0026_module_imports():
    module = _load_migration_0026_module()
    assert module.revision == "0026"
    assert module.down_revision == "0025"
    assert module.branch_labels is None
    assert module.depends_on is None
    assert hasattr(module, "upgrade")
    assert hasattr(module, "downgrade")


def test_migration_0026_has_eleven_stages():
    module = _load_migration_0026_module()
    stages = module.COLLAGE_STAGES
    assert len(stages) == 11
    json.dumps(stages)  # must be JSON-serialisable (written as JSONB)
    for st in stages:
        for key in (
            "label", "prompt", "width", "height",
            "grid_x", "grid_y", "inset_pct", "reference_policy",
        ):
            assert key in st, f"missing {key}"


def test_migration_0026_stages_1_to_3_are_byte_identical_to_0025():
    """Stages 1-3 (face rotation, expressions, angles/upper-body/wardrobe)
    must be copied byte-for-byte from 0025 — same identity-locking grid
    recipe, no per-stage model override."""
    prev = _load_migration_0025_module()
    curr = _load_migration_0026_module()
    prev_stages = prev.COLLAGE_STAGES[0:3]
    curr_stages = curr.COLLAGE_STAGES[0:3]
    assert curr_stages == prev_stages
    for st in curr_stages:
        assert "model" not in st


def test_migration_0026_single_stages_share_seedream_geometry():
    """Stages 4-11 are single full-body photos: 2048x2048, 1x1 'grid' (one
    frame, not a collage), no inset cropping, generated with the per-stage
    Seedream model override."""
    module = _load_migration_0026_module()
    for stage in module.COLLAGE_STAGES[3:11]:
        assert (stage["width"], stage["height"]) == (2048, 2048)
        assert (stage["grid_x"], stage["grid_y"]) == (1, 1)
        assert stage["inset_pct"] == 0.0
        assert stage["model"] == "bytedance:seedream@5.0-pro"


def test_migration_0026_single_stage_reference_policies():
    """Stage 4 chains off the identity collage; stages 5-11 chain off every
    prior collage/single so later poses can draw on the growing reference
    set."""
    module = _load_migration_0026_module()
    stages = module.COLLAGE_STAGES
    assert stages[3]["reference_policy"] == "collage_1"
    for stage in stages[4:11]:
        assert stage["reference_policy"] == "all_prior"


def test_migration_0026_single_prompts_are_single_photo_not_grid():
    module = _load_migration_0026_module()
    for stage in module.COLLAGE_STAGES[3:11]:
        assert "ONE single photo" in stage["prompt"], f"missing in {stage['label']}"
        assert "grid of" not in stage["prompt"], f"unexpected in {stage['label']}"


def test_migration_0026_single_prompts_cover_both_orientations():
    """Orientation coverage: at least 3 prompts turn the subject toward the
    RIGHT edge and at least 3 toward the LEFT edge."""
    module = _load_migration_0026_module()
    single_prompts = [st["prompt"] for st in module.COLLAGE_STAGES[3:11]]
    right_hits = sum(1 for p in single_prompts if "RIGHT" in p)
    left_hits = sum(1 for p in single_prompts if "LEFT" in p)
    assert right_hits >= 3, f"only {right_hits} RIGHT-oriented prompts"
    assert left_hits >= 3, f"only {left_hits} LEFT-oriented prompts"


def test_migration_0026_collage_prompt_matches_0020_zimage_fallback():
    """collage_prompt is only a fallback (stages take precedence); it must
    match the legacy Z-Image prompt seeded by 0020."""
    m0020 = _load_migration_module()
    m0026 = _load_migration_0026_module()
    assert m0026.COLLAGE_PROMPT == m0020.IMPROVED_COLLAGE_PROMPT


def test_migration_0026_upgrade_is_idempotent_insert():
    src = MIGRATION_0026_PATH.read_text()
    assert "INSERT INTO dataset_templates" in src
    assert "NOT EXISTS" in src
    assert "Identity Collages + Full-body Singles — Z-Image" in src


def test_migration_0026_downgrade_deletes_by_name():
    src = MIGRATION_0026_PATH.read_text()
    assert "DELETE FROM dataset_templates" in src
    assert "def downgrade" in src


# ── schema + store round-trip: model_target on Out/Create/Update ──────────

_ID = "00000000-0000-0000-0000-0000000000aa"
_USER = "00000000-0000-0000-0000-000000000001"


class _FakeTemplateRow:
    """Stand-in for a DatasetTemplate ORM row with every column populated, so
    ``DatasetTemplateOut.model_validate`` (from_attributes) round-trips it."""

    def __init__(self, **over):
        now = datetime.now(timezone.utc)
        self.id = _ID
        self.user_id = None
        self.name = "T"
        self.description = None
        self.collage_prompt = "p"
        self.collage_model = "openai:gpt-image@2"
        self.collage_width = 3840
        self.collage_height = 2160
        self.collage_quality = "high"
        self.split_grid_x = 4
        self.split_grid_y = 4
        self.upscale_enabled = True
        self.upscale_model = "prunaai:p-image@upscale"
        self.target_megapixels = 4
        self.upscale_enhance_details = False
        self.upscale_realism = False
        self.caption_vision_model = "google/gemini-2.5-flash"
        self.caption_format = "{{trigger_token}}, {{description}}"
        self.model_target = "sdxl"
        self.collage_stages = None
        self.seed_reference_media_id = None
        self.is_default = False
        self.created_at = now
        self.updated_at = now
        for k, v in over.items():
            setattr(self, k, v)


async def _refresh_server_defaults(row):
    """Emulate refresh() repopulating server-default id/timestamp columns."""
    if getattr(row, "id", None) is None:
        row.id = _ID
    now = datetime.now(timezone.utc)
    row.created_at = now
    row.updated_at = now


# schema-level defaults


def test_out_defaults_model_target_sdxl():
    now = datetime.now(timezone.utc)
    out = DatasetTemplateOut(
        id=_ID, name="n", collage_prompt="p", created_at=now, updated_at=now
    )
    assert out.model_target == "sdxl"


def test_create_defaults_model_target_sdxl():
    assert DatasetTemplateCreate(name="n", collage_prompt="p").model_target == "sdxl"


def test_update_model_target_optional_none_by_default():
    assert DatasetTemplateUpdate().model_target is None


# store-level round-trip (mocked session, exercises the real store wiring)


@pytest.mark.asyncio
async def test_create_template_persists_model_target():
    session = MagicMock()
    session.commit = AsyncMock()
    session.refresh = AsyncMock(side_effect=_refresh_server_defaults)
    added = []
    session.add = MagicMock(side_effect=lambda r: added.append(r))

    body = DatasetTemplateCreate(name="n", collage_prompt="p", model_target="z-image")
    out = await dataset_templates.create_template(session, body, user_id=_USER)

    assert added[0].model_target == "z-image"
    assert out.model_target == "z-image"


@pytest.mark.asyncio
async def test_create_template_defaults_model_target_sdxl():
    session = MagicMock()
    session.commit = AsyncMock()
    session.refresh = AsyncMock(side_effect=_refresh_server_defaults)
    added = []
    session.add = MagicMock(side_effect=lambda r: added.append(r))

    body = DatasetTemplateCreate(name="n", collage_prompt="p")
    out = await dataset_templates.create_template(session, body, user_id=_USER)

    assert added[0].model_target == "sdxl"
    assert out.model_target == "sdxl"


@pytest.mark.asyncio
async def test_update_template_applies_model_target():
    row = _FakeTemplateRow(model_target="sdxl")
    session = MagicMock()
    session.get = AsyncMock(return_value=row)
    session.commit = AsyncMock()
    session.refresh = AsyncMock()

    out = await dataset_templates.update_template(
        session, _ID, DatasetTemplateUpdate(model_target="z-image"), user_id=None
    )

    assert row.model_target == "z-image"
    assert out.model_target == "z-image"


@pytest.mark.asyncio
async def test_update_template_leaves_model_target_when_absent():
    row = _FakeTemplateRow(model_target="z-image")
    session = MagicMock()
    session.get = AsyncMock(return_value=row)
    session.commit = AsyncMock()
    session.refresh = AsyncMock()

    # partial patch that does not include model_target must not clobber it
    out = await dataset_templates.update_template(
        session, _ID, DatasetTemplateUpdate(name="renamed"), user_id=None
    )

    assert row.model_target == "z-image"
    assert out.model_target == "z-image"


# ── schema + store round-trip: collage_stages + seed_reference_media_id ────

# A single stage dict (purpose/model included so model_dump round-trips
# exactly — CollageStage.model is the optional per-stage image-generation
# model override).
_STAGE = {
    "label": "Identity — Face Rotation",
    "purpose": None,
    "prompt": "a seamless 4x4 grid ...",
    "width": 2048,
    "height": 2048,
    "grid_x": 4,
    "grid_y": 4,
    "inset_pct": 0.015,
    "model": None,
    "reference_policy": "identity_seed",
}


def test_out_defaults_collage_fields_none():
    now = datetime.now(timezone.utc)
    out = DatasetTemplateOut(
        id=_ID, name="n", collage_prompt="p", created_at=now, updated_at=now
    )
    assert out.collage_stages is None
    assert out.seed_reference_media_id is None


def test_out_coerces_jsonb_dicts_into_collage_stages():
    """DB JSONB returns plain dicts; Out must validate them as CollageStage."""
    now = datetime.now(timezone.utc)
    out = DatasetTemplateOut(
        id=_ID, name="n", collage_prompt="p", created_at=now, updated_at=now,
        collage_stages=[_STAGE],
    )
    assert len(out.collage_stages) == 1
    assert out.collage_stages[0].label == "Identity — Face Rotation"
    assert out.collage_stages[0].grid_x == 4


@pytest.mark.asyncio
async def test_create_template_persists_collage_stages_and_seed_ref():
    session = MagicMock()
    session.commit = AsyncMock()
    session.refresh = AsyncMock(side_effect=_refresh_server_defaults)
    added = []
    session.add = MagicMock(side_effect=lambda r: added.append(r))

    body = DatasetTemplateCreate(
        name="n",
        collage_prompt="p",
        collage_stages=[_STAGE],
        seed_reference_media_id="media-abc",
    )
    out = await dataset_templates.create_template(session, body, user_id=_USER)

    # persisted onto the ORM object as plain JSON-safe dicts (not pydantic models)
    assert added[0].collage_stages == [_STAGE]
    assert added[0].seed_reference_media_id == "media-abc"
    # and round-trips back out as CollageStage models
    assert out.seed_reference_media_id == "media-abc"
    assert out.collage_stages[0].model_dump() == _STAGE


@pytest.mark.asyncio
async def test_create_template_defaults_collage_fields_none():
    session = MagicMock()
    session.commit = AsyncMock()
    session.refresh = AsyncMock(side_effect=_refresh_server_defaults)
    added = []
    session.add = MagicMock(side_effect=lambda r: added.append(r))

    body = DatasetTemplateCreate(name="n", collage_prompt="p")
    out = await dataset_templates.create_template(session, body, user_id=_USER)

    assert added[0].collage_stages is None
    assert added[0].seed_reference_media_id is None
    assert out.collage_stages is None
    assert out.seed_reference_media_id is None


@pytest.mark.asyncio
async def test_update_template_applies_collage_stages_and_seed_ref():
    row = _FakeTemplateRow(collage_stages=None, seed_reference_media_id=None)
    session = MagicMock()
    session.get = AsyncMock(return_value=row)
    session.commit = AsyncMock()
    session.refresh = AsyncMock()

    out = await dataset_templates.update_template(
        session,
        _ID,
        DatasetTemplateUpdate(
            collage_stages=[_STAGE], seed_reference_media_id="media-xyz"
        ),
        user_id=None,
    )

    # stored as plain dicts; merged onto the row
    assert row.collage_stages == [_STAGE]
    assert row.seed_reference_media_id == "media-xyz"
    assert out.collage_stages[0].label == "Identity — Face Rotation"
    assert out.seed_reference_media_id == "media-xyz"


@pytest.mark.asyncio
async def test_update_template_leaves_collage_fields_when_absent():
    row = _FakeTemplateRow(
        collage_stages=[_STAGE], seed_reference_media_id="keep-me"
    )
    session = MagicMock()
    session.get = AsyncMock(return_value=row)
    session.commit = AsyncMock()
    session.refresh = AsyncMock()

    # partial patch omitting the collage fields must not clobber them
    out = await dataset_templates.update_template(
        session, _ID, DatasetTemplateUpdate(name="renamed"), user_id=None
    )

    assert row.collage_stages == [_STAGE]
    assert row.seed_reference_media_id == "keep-me"
    assert out.collage_stages[0].label == "Identity — Face Rotation"
    assert out.seed_reference_media_id == "keep-me"


# ── CollageStage.model: per-stage image-generation model override ─────────


def test_collage_stage_model_defaults_none():
    stage = CollageStage(
        label="l", prompt="p", width=1, height=1, grid_x=1, grid_y=1,
        reference_policy="collage_1",
    )
    assert stage.model is None


@pytest.mark.asyncio
async def test_create_template_persists_collage_stage_model_override():
    """A CollageStage with a per-stage model override must survive a
    create/read round-trip through the store (the Go backend prefers this
    over the template's collage_model)."""
    session = MagicMock()
    session.commit = AsyncMock()
    session.refresh = AsyncMock(side_effect=_refresh_server_defaults)
    added = []
    session.add = MagicMock(side_effect=lambda r: added.append(r))

    stage_with_model = {**_STAGE, "model": "bytedance:seedream@5.0-pro"}
    body = DatasetTemplateCreate(
        name="n",
        collage_prompt="p",
        collage_stages=[CollageStage(**stage_with_model)],
    )
    out = await dataset_templates.create_template(session, body, user_id=_USER)

    assert added[0].collage_stages == [stage_with_model]
    assert out.collage_stages[0].model == "bytedance:seedream@5.0-pro"
