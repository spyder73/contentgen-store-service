"""dataset_template: body-reference collage stage + explicit reference wiring

Inserts a dedicated "body reference" stage into the SYSTEM
`Identity Collages + Full-body Singles — Z-Image` preset (the `user_id IS NULL`
row seeded by 0026) and rewires the full-body single stages to reference it.

Motivation: 0026's single full-body stages chained references via the loose
`all_prior` policy, so a single could draw on ANY prior collage/single and the
subject's *body build* drifted between shots. 0027 adds ONE new stage — a 2x2
grid of 4 full-length photos whose sole job is to lock her true body shape and
proportions — and points every subsequent single at exactly two references:
the stage-1 face-rotation grid (identity) and the new stage-4 body grid (build),
via the new `reference_stage_indexes` wiring (explicit 1-based stage indexes,
which take precedence over each single's `reference_policy` string when they
yield a match — the policy stays as the fallback).

12-stage recipe (was 11 in 0026):
  * Stages 1-3: byte-for-byte the 0025 identity/expression/upper-body grids
    (0026 already reused these verbatim).
  * Stage 4 (NEW): "Full-body Reference (2×2)" — a 2048x2048 2x2 grid of 4
    full-length photos, default template model (gpt-image), chained off the
    identity collage (`collage_1`).
  * Stages 5-12: the 8 single full-body shots copied verbatim from 0026
    (labels/prompts/geometry/`model` unchanged) with `reference_stage_indexes`
    = [1, 4] added to each (face grid + body grid).

Both recipes are DERIVED by loading the 0025 and 0026 migration modules by file
path (the numeric-prefixed filenames aren't importable package names), so the
copied stages are guaranteed byte-identical to their sources — the same
importlib pattern the migration tests use. Data-content update only (no schema
change): the `reference_stage_indexes` list round-trips through the existing
JSONB `collage_stages` column and `CollageStage.reference_stage_indexes` in
app/schemas.py.

Idempotent: the upgrade re-sets the 12-stage recipe on the one target system
row (narrowed by `user_id IS NULL AND name`). Downgrade restores 0026's exact
11-stage recipe on that row.

revision: 0027
down_revision: 0026
"""
from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None

TEMPLATE_NAME = "Identity Collages + Full-body Singles — Z-Image"

_VERSIONS_DIR = Path(__file__).resolve().parent


def _load_sibling(filename: str, module_name: str):
    """Load a sibling migration module by file path so its COLLAGE_STAGES can be
    reused verbatim (the numeric-prefixed filenames aren't valid import names)."""
    spec = importlib.util.spec_from_file_location(
        module_name, _VERSIONS_DIR / filename
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_M0025 = _load_sibling(
    "0025_dataset_template_lean_prompts.py",
    "migration_0027_src_0025",
)
_M0026 = _load_sibling(
    "0026_dataset_template_fullbody_singles.py",
    "migration_0027_src_0026",
)

# Stage 4 (NEW): a 2x2 grid of 4 full-length photos whose only job is to lock her
# real body build/height/proportions before the single full-body shots draw on
# it. Default template model (no `model` key → gpt-image), chained off the
# identity collage.
_STAGE_4_BODY_REFERENCE_PROMPT = """Every reference image shows the same woman. Match her face, hair, and skin exactly — and treat her body build and proportions as fixed identity.

Create ONE photo: a seamless 2x2 grid of 4 equal square tiles, each a complete standalone FULL-LENGTH photo of her from head to toe — her whole body visible, filled edge-to-edge — no borders, gutters, text, or logos anywhere (the image is sliced into 4 separate training photos).

This grid establishes her true body shape, so keep her build, height impression, and proportions identical and realistic in all 4 tiles:
- Tile 1: form-fitting sportswear — sports bra or fitted athletic top and leggings — standing straight-on facing the camera, arms relaxed. This tile defines her body proportions most clearly.
- Tile 2: fitted jeans and a tucked-in top, three-quarter stance turned toward the RIGHT edge of the frame, walking casually.
- Tile 3: an elegant fitted midi dress, three-quarter stance turned toward the LEFT edge of the frame, one hand on hip.
- Tile 4: athleisure — leggings and a fitted zip jacket — seen from BEHIND (full rear view, head-to-toe silhouette).

Each tile: a different simple background (studio, street, interior, park), soft even light, the full body always in frame with a little headroom and floor visible. Her face is sharp and recognizable in the three front-facing tiles.

Photorealistic, natural skin and fabric detail, the same woman with the same body in all 4 tiles."""

STAGE_4_BODY_REFERENCE = {
    "label": "Full-body Reference (2×2)",
    "prompt": _STAGE_4_BODY_REFERENCE_PROMPT,
    "width": 2048,
    "height": 2048,
    "grid_x": 2,
    "grid_y": 2,
    "inset_pct": 0.015,
    "reference_policy": "collage_1",
}

# Explicit reference wiring for the single full-body stages: the stage-1 face
# rotation grid (identity) + the new stage-4 body grid (build), both 1-based.
_SINGLE_REFERENCE_STAGE_INDEXES = [1, 4]


def _twelve_stage_recipe() -> list[dict]:
    # Stages 1-3: the 0025 identity/expression/upper-body grids, verbatim.
    grids = copy.deepcopy(_M0025.COLLAGE_STAGES[0:3])
    # Stages 5-12: 0026's 8 single full-body shots, each rewired to reference the
    # face grid (stage 1) and the new body grid (stage 4).
    singles = [
        {**copy.deepcopy(single), "reference_stage_indexes": list(_SINGLE_REFERENCE_STAGE_INDEXES)}
        for single in _M0026.COLLAGE_STAGES[3:11]
    ]
    return [*grids, copy.deepcopy(STAGE_4_BODY_REFERENCE), *singles]


# 12-stage recipe applied by upgrade; 0026's exact 11-stage recipe restored by
# downgrade.
COLLAGE_STAGES = _twelve_stage_recipe()
COLLAGE_STAGES_0026 = copy.deepcopy(_M0026.COLLAGE_STAGES)


def upgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE dataset_templates SET collage_stages = CAST(:stages AS jsonb) "
            "WHERE user_id IS NULL AND name = :name"
        ).bindparams(
            sa.bindparam("stages", value=json.dumps(COLLAGE_STAGES)),
            sa.bindparam("name", value=TEMPLATE_NAME),
        )
    )


def downgrade() -> None:
    # Restore 0026's exact 11-stage recipe on the same system row.
    op.execute(
        sa.text(
            "UPDATE dataset_templates SET collage_stages = CAST(:stages AS jsonb) "
            "WHERE user_id IS NULL AND name = :name"
        ).bindparams(
            sa.bindparam("stages", value=json.dumps(COLLAGE_STAGES_0026)),
            sa.bindparam("name", value=TEMPLATE_NAME),
        )
    )
