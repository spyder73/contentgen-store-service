"""dataset_template: Identity Collages + Full-body Singles (Z-Image) preset

Adds a new SYSTEM `dataset_templates` row (`user_id IS NULL`) that keeps the
proven 0025 grid-collage stages 1-3 (face rotation, expressions, angles /
upper-body / wardrobe / backgrounds) for identity locking, then replaces the
3x2 full-body GRID stages with 8 dedicated SINGLE-photo full-body stages —
one subject per image, generated with a per-stage model override
(`bytedance:seedream@5.0-pro`) instead of the grid-collage image model. Grid
tiles compress full-body detail into a fraction of the frame; single shots
let the per-stage model spend its whole output resolution on one pose, which
is expected to produce sharper, more identity-consistent full-body training
images.

Stages 1-3 are copied byte-for-byte from 0025's COLLAGE_STAGES (same prompts,
same 2048x2048 4x4 grid geometry, no `model` key — they keep using the
template's `collage_model`). Stages 4-11 are new: 2048x2048, 1x1 "grid"
(i.e. one full-frame photo, not a collage), `inset_pct` 0.0 (no cropping —
the whole frame is the training image), and `model` set to the per-stage
Seedream override. Stage 4 chains off the identity collage from stage 1
(`reference_policy = "collage_1"`); stages 5-11 chain off every prior
collage/single so later poses can draw on the growing reference set
(`reference_policy = "all_prior"`).

This is purely an additive INSERT (idempotent — skipped if a row with this
name already exists) — no existing row is modified and no schema change is
needed (the `collage_stages` JSONB column and the `model` key on each stage
dict already round-trip via `CollageStage.model` in app/schemas.py).

revision: 0026
down_revision: 0025
"""
from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None

TEMPLATE_NAME = "Identity Collages + Full-body Singles — Z-Image"

# Fallback-only prompt (stages take precedence); this is the same legacy
# collage prompt seeded by 0020 for the Z-Image system row.
COLLAGE_PROMPT = """Using the attached reference image(s) as the ground-truth facial identity (they define the exact face — override any conflicting detail), generate ONE high-resolution image containing a clean 4x4 grid (16 tiles) of the SAME person.

CRITICAL GRID FORMAT: a seamless 4x4 grid of 16 equal-size rectangular tiles, each tile filled edge-to-edge with its own photo. NO borders, gutters, gaps, frames, lines, numbers, captions, or text anywhere in the image. Consistent tile size. This grid will be sliced into 16 separate training images, so every tile must be a complete standalone photo of the person.

LOCK (identical in every tile — this is the identity to learn): facial bone structure, eye shape and color, nose, lips, skin tone, and ALL skin characteristics (freckles, moles, scars, pores, texture); hair color, strand texture, natural shine, thickness, and length.

VARY across the 16 tiles — make every tile clearly distinct:
- ANGLE & FRAMING: frontal, 3/4 left and right, both side profiles, gentle overhead and low upward angles, and a couple of dramatic high/low camera angles. Distribute distances: about 5 extreme close-ups (face fills the frame, pores/eyelashes sharp), about 6 standard head-and-shoulders headshots, about 3 wider waist-up shots, and about 2 FULL-BODY shots (head to toe, showing posture and physique).
- EXPRESSION: mix neutral, a natural soft smile, and a few candid/animated expressions — do not repeat the same expression across tiles.
- HAIRSTYLE ARRANGEMENT (styling only — never change color or length): rotate through down, soft waves, half-up, low bun, high ponytail, side braid, tucked behind one ear, center and side parts, slightly windblown.
- LIGHTING: soft daylight, warm golden hour, neutral studio key, dramatic Rembrandt/side light, and rim/backlight — a different lighting mood per tile.
- BACKGROUND / SETTING: give EACH tile a DIFFERENT background — plain studio seamless in varying colors, soft outdoor bokeh (park, street, beach), indoor rooms (cafe, apartment, studio), and neutral gradients. Do NOT reuse one backdrop across tiles; a constant background gets wrongly baked into the character.
- WARDROBE: vary the outfit per tile (casual, smart, seasonal) while keeping it natural for the person.
- LENS & DEPTH: mix macro/beauty, 85mm portrait compression, and 35mm wider framing; vary depth of field (some sharp throughout, some soft bokeh); include 1-2 subtle dutch tilts.

QUALITY: photorealistic, sharp, high skin-texture detail, natural color. No text, watermarks, logos, borders, or tile numbering anywhere. The person must be instantly recognizable as the SAME individual in all 16 tiles.

Render at the maximum available resolution."""

# ── Stages 1-3: byte-for-byte copies of 0025's COLLAGE_STAGES[0:3] ──────────
_STAGE_1_PROMPT = """The reference images show one woman — match her face, hair, and skin exactly in every tile.

Create ONE photo: a seamless 4x4 grid of 16 equal square tiles, each a complete standalone close-up photo of this same woman, filled edge-to-edge — no borders, gutters, text, or logos anywhere (the image is sliced into 16 separate training photos).

Tile plan, row by row (left to right) — head directions are anchored to the frame edges:
- Row 1: full profile, nose pointing at the RIGHT frame edge; three-quarter view toward the RIGHT edge; straight-on frontal; three-quarter view toward the LEFT edge.
- Row 2: full profile toward the LEFT edge; rear three-quarter over her right shoulder; straight back of head; rear three-quarter over her left shoulder.
- Row 3: HIGH camera looking down, frontal; HIGH camera looking down, head turned toward the RIGHT edge; LOW camera looking up, frontal; LOW camera looking up, head turned toward the LEFT edge.
- Row 4: extreme macro (face fills 90%+), frontal; extreme macro, turned toward the RIGHT edge; head-and-shoulders, frontal; head-and-shoulders, turned toward the LEFT edge.

Turned poses come in mirrored pairs — exactly as many toward the right edge as toward the left edge.

Framing: mix extreme macro (face 85-100%), standard close-up (60-75%), and head-and-shoulders (45-60%) as assigned above. Lighting varies per tile: soft daylight, golden hour, studio key, Rembrandt side light, rim light. Hairstyle arrangement varies (down, half-up, bun, ponytail, braid, tucked behind ear); hair color and length never change. Expression neutral and calm. Background: plain neutral studio.

Photorealistic, sharp, maximum skin detail, the same woman instantly recognizable in all 16 tiles."""

_STAGE_2_PROMPT = """Every reference image shows the same woman — the grid reference shows her from multiple angles. Match her face, hair, and skin exactly.

Create ONE photo: a seamless 4x4 grid of 16 equal square tiles, each a complete standalone photo, filled edge-to-edge — no borders, gutters, text, or logos anywhere (the image is sliced into 16 separate training photos).

One expression per tile: laughing; shocked/surprised; pouty/sad; angry/intense; confused/thinking; smug playful smirk; sleepy/relaxed; mysterious/subtle; tongue out; winking; blowing a kiss; head tilted; over-the-shoulder glance; cheeks puffed; two expressive candids.

Head direction rotates tile to tile in mirrored pairs — exactly as many heads turned toward the RIGHT frame edge as toward the left edge, the rest frontal; include 2 high-angle tiles (camera above) and 2 low-angle tiles (camera below).

Face fills 60-75% of each tile (a couple of tighter beauty macros allowed); vary lens and depth of field, 1-2 subtle dutch tilts. Even professional lighting; lightly varied neutral backgrounds. Hairstyle arrangement may vary; hair color and length never change.

Photorealistic, sharp, high skin detail, the same woman instantly recognizable in all 16 tiles."""

_STAGE_3_PROMPT = """Every reference image shows the same woman. Match her face, hair, and skin exactly.

Create ONE photo: a seamless 4x4 grid of 16 equal square tiles, each a complete standalone photo of her from the chest/shoulders up, filled edge-to-edge — no borders, gutters, text, or logos anywhere (the image is sliced into 16 separate training photos).

Body and head directions in mirrored pairs anchored to the frame edges: equal counts of three-quarter turns toward the RIGHT edge and toward the left edge; one full profile facing the RIGHT edge and one facing the left edge; one rear three-quarter over each shoulder; the rest frontal. Include 2 high-angle and 2 low-angle tiles.

EACH tile: a DIFFERENT simple everyday outfit (crewneck tee, blouse, light sweater, denim or utility jacket, turtleneck — relaxed natural fit) AND a DIFFERENT background (plain studio in varied colors; outdoor bokeh — park, street, cafe; simple indoor rooms). Arms vary: relaxed at sides, crossed, one hand near face or hair, hands clasped, adjusting collar.

Framing mixes tight head-and-shoulders with looser chest-up; the face stays sharp and at least ~35% of every frame. Hairstyle arrangement varies; hair color and length never change.

Photorealistic, sharp, the same woman instantly recognizable in all 16 tiles."""

# ── Stages 4-11: dedicated single full-body shots (Seedream per-stage) ─────
_STAGE_4_PROMPT = """ONE single photo (not a grid, not a collage): the same woman as in the reference images — match her face, hair, skin, and natural body proportions exactly.

Three-quarter body shot from the knees up: she stands in an elegant evening interior wearing a tailored blazer over a cocktail dress, body and gaze angled toward the RIGHT edge of the frame, one hand adjusting her lapel. Warm interior key light, background softly blurred.

Her face is sharp, clearly resolved, and instantly recognizable. Photorealistic, natural skin detail. No text, watermarks, logos, or borders."""

_STAGE_5_PROMPT = """ONE single photo (not a grid, not a collage): the same woman as in the reference images — match her face, hair, skin, and natural body proportions exactly.

Full-length head-to-toe: walking mid-stride across an urban crosswalk in streetwear — hoodie, jeans, sneakers — body moving toward the LEFT edge of the frame, candid gaze ahead. Overcast daylight, street background softly blurred.

Her face is sharp, clearly resolved, and instantly recognizable even at this distance. Photorealistic, natural skin detail. No text, watermarks, logos, or borders."""

_STAGE_6_PROMPT = """ONE single photo (not a grid, not a collage): the same woman as in the reference images — match her face, hair, skin, and natural body proportions exactly.

Full-length head-to-toe: outdoor park, post-run stretch in athleisure leggings and a zip jacket, facing the camera straight-on, arms reaching overhead. Bright morning light.

Her face is sharp, clearly resolved, and instantly recognizable even at this distance. Photorealistic, natural skin detail. No text, watermarks, logos, or borders."""

_STAGE_7_PROMPT = """ONE single photo (not a grid, not a collage): the same woman as in the reference images — match her face, hair, skin, and natural body proportions exactly.

Three-quarter body shot from the knees up: seated at a cafe terrace table in a summer sundress, body angled toward the RIGHT edge of the frame, holding a coffee cup with a relaxed smile. Golden-hour sunlight.

Her face is sharp, clearly resolved, and instantly recognizable. Photorealistic, natural skin detail. No text, watermarks, logos, or borders."""

_STAGE_8_PROMPT = """ONE single photo (not a grid, not a collage): the same woman as in the reference images — match her face, hair, skin, and natural body proportions exactly.

Full-length head-to-toe: a bright office lobby, business-casual blouse and slacks, walking away from the camera while glancing back over her RIGHT shoulder directly at the lens. Cool daylight through glass.

Her face is sharp, clearly resolved, and instantly recognizable even at this distance. Photorealistic, natural skin detail. No text, watermarks, logos, or borders."""

_STAGE_9_PROMPT = """ONE single photo (not a grid, not a collage): the same woman as in the reference images — match her face, hair, skin, and natural body proportions exactly.

Thigh-up shot: a home kitchen, comfy oversized knit sweater and leggings, in side profile facing the LEFT edge of the frame while she stirs a pot on the stove. Soft window light.

Her face is sharp, clearly resolved, and instantly recognizable. Photorealistic, natural skin detail. No text, watermarks, logos, or borders."""

_STAGE_10_PROMPT = """ONE single photo (not a grid, not a collage): the same woman as in the reference images — match her face, hair, skin, and natural body proportions exactly.

Full-length head-to-toe: browsing a street market in casual chic — light jacket and jeans — three-quarter view turned toward the RIGHT edge of the frame, reaching for fruit on a stall. Lively afternoon light.

Her face is sharp, clearly resolved, and instantly recognizable even at this distance. Photorealistic, natural skin detail. No text, watermarks, logos, or borders."""

_STAGE_11_PROMPT = """ONE single photo (not a grid, not a collage): the same woman as in the reference images — match her face, hair, skin, and natural body proportions exactly.

Full-body shot: seated on a yoga mat at home in athletic wear, holding a seated twist pose, torso toward the camera and head turned toward the LEFT edge of the frame. Calm morning light.

Her face is sharp, clearly resolved, and instantly recognizable. Photorealistic, natural skin detail. No text, watermarks, logos, or borders."""


SINGLE_STAGE_MODEL = "bytedance:seedream@5.0-pro"

COLLAGE_STAGES = [
    {
        "label": "Identity — Face Rotation",
        "prompt": _STAGE_1_PROMPT,
        "width": 2048,
        "height": 2048,
        "grid_x": 4,
        "grid_y": 4,
        "inset_pct": 0.015,
        "reference_policy": "identity_seed",
    },
    {
        "label": "Expressions & Emotions",
        "prompt": _STAGE_2_PROMPT,
        "width": 2048,
        "height": 2048,
        "grid_x": 4,
        "grid_y": 4,
        "inset_pct": 0.015,
        "reference_policy": "collage_1",
    },
    {
        "label": "Angles · Upper-body · Wardrobe · Backgrounds",
        "prompt": _STAGE_3_PROMPT,
        "width": 2048,
        "height": 2048,
        "grid_x": 4,
        "grid_y": 4,
        "inset_pct": 0.015,
        "reference_policy": "collage_1",
    },
    {
        "label": "FB Single — Formal (right)",
        "prompt": _STAGE_4_PROMPT,
        "width": 2048,
        "height": 2048,
        "grid_x": 1,
        "grid_y": 1,
        "inset_pct": 0.0,
        "reference_policy": "collage_1",
        "model": SINGLE_STAGE_MODEL,
    },
    {
        "label": "FB Single — Streetwear (left)",
        "prompt": _STAGE_5_PROMPT,
        "width": 2048,
        "height": 2048,
        "grid_x": 1,
        "grid_y": 1,
        "inset_pct": 0.0,
        "reference_policy": "all_prior",
        "model": SINGLE_STAGE_MODEL,
    },
    {
        "label": "FB Single — Athleisure (frontal)",
        "prompt": _STAGE_6_PROMPT,
        "width": 2048,
        "height": 2048,
        "grid_x": 1,
        "grid_y": 1,
        "inset_pct": 0.0,
        "reference_policy": "all_prior",
        "model": SINGLE_STAGE_MODEL,
    },
    {
        "label": "FB Single — Summer (right)",
        "prompt": _STAGE_7_PROMPT,
        "width": 2048,
        "height": 2048,
        "grid_x": 1,
        "grid_y": 1,
        "inset_pct": 0.0,
        "reference_policy": "all_prior",
        "model": SINGLE_STAGE_MODEL,
    },
    {
        "label": "FB Single — Business (rear glance)",
        "prompt": _STAGE_8_PROMPT,
        "width": 2048,
        "height": 2048,
        "grid_x": 1,
        "grid_y": 1,
        "inset_pct": 0.0,
        "reference_policy": "all_prior",
        "model": SINGLE_STAGE_MODEL,
    },
    {
        "label": "FB Single — Home (left profile)",
        "prompt": _STAGE_9_PROMPT,
        "width": 2048,
        "height": 2048,
        "grid_x": 1,
        "grid_y": 1,
        "inset_pct": 0.0,
        "reference_policy": "all_prior",
        "model": SINGLE_STAGE_MODEL,
    },
    {
        "label": "FB Single — Market (right)",
        "prompt": _STAGE_10_PROMPT,
        "width": 2048,
        "height": 2048,
        "grid_x": 1,
        "grid_y": 1,
        "inset_pct": 0.0,
        "reference_policy": "all_prior",
        "model": SINGLE_STAGE_MODEL,
    },
    {
        "label": "FB Single — Yoga (left)",
        "prompt": _STAGE_11_PROMPT,
        "width": 2048,
        "height": 2048,
        "grid_x": 1,
        "grid_y": 1,
        "inset_pct": 0.0,
        "reference_policy": "all_prior",
        "model": SINGLE_STAGE_MODEL,
    },
]


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            INSERT INTO dataset_templates (
                user_id, name, description, collage_prompt, collage_model,
                collage_width, collage_height, collage_quality,
                split_grid_x, split_grid_y, upscale_enabled, upscale_model,
                target_megapixels, caption_vision_model, caption_format,
                model_target, collage_stages, is_default
            )
            SELECT
                NULL, :name, :description, :collage_prompt, :collage_model,
                :collage_width, :collage_height, :collage_quality,
                :split_grid_x, :split_grid_y, :upscale_enabled, :upscale_model,
                :target_megapixels, :caption_vision_model, :caption_format,
                :model_target, CAST(:collage_stages AS jsonb), :is_default
            WHERE NOT EXISTS (
                SELECT 1 FROM dataset_templates WHERE name = :name
            )
            """
        ).bindparams(
            sa.bindparam("name", value=TEMPLATE_NAME),
            sa.bindparam(
                "description",
                value=(
                    "Face/expression/upper-body collages plus single "
                    "full-body shots (Seedream per-stage) — Z-Image "
                    "natural-language captions"
                ),
            ),
            sa.bindparam("collage_prompt", value=COLLAGE_PROMPT),
            sa.bindparam("collage_model", value="openai:gpt-image@2"),
            sa.bindparam("collage_width", value=2048),
            sa.bindparam("collage_height", value=2048),
            sa.bindparam("collage_quality", value="high"),
            sa.bindparam("split_grid_x", value=4),
            sa.bindparam("split_grid_y", value=4),
            sa.bindparam("upscale_enabled", value=True),
            sa.bindparam("upscale_model", value="prunaai:p-image@upscale"),
            sa.bindparam("target_megapixels", value=4),
            sa.bindparam("caption_vision_model", value="google/gemini-2.5-flash"),
            sa.bindparam(
                "caption_format", value="A photo of {{trigger_token}}, {{description}}."
            ),
            sa.bindparam("model_target", value="z-image"),
            sa.bindparam("collage_stages", value=json.dumps(COLLAGE_STAGES)),
            sa.bindparam("is_default", value=False),
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text("DELETE FROM dataset_templates WHERE name = :name").bindparams(
            sa.bindparam("name", value=TEMPLATE_NAME)
        )
    )
