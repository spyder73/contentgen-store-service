"""dataset template collage stages

Revision ID: 0022
Revises: 0021
Create Date: 2026-07-09

Adds the multi-stage collage recipe to `dataset_templates`:

  * `collage_stages` (JSONB, NULL) — an ordered list of stage dicts
    (label/prompt/width/height/grid_x/grid_y/inset_pct/reference_policy).
    NULL means legacy single-prompt mode (`collage_prompt` + `split_grid_*`).
  * `seed_reference_media_id` (Text, NULL) — the avatar/seed reference chained
    forward across collages for identity consistency.

It then seeds the 4-stage recipe onto BOTH system rows (`user_id IS NULL`):
the SDXL default and the Z-Image preset. The collage prompts are identical for
both — `model_target` only affects captioning/training, not collage generation.

The recipe is defined here as a SELF-CONTAINED module-level literal on purpose:
migrations must NOT import app code (a prior bug came from a migration importing
app schemas).
"""
from __future__ import annotations

import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0022"
down_revision: Union[str, None] = "0021"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ── The 4-stage collage recipe (Part A of the Dataset Studio plan) ────────────
# Plain-text prompts (no Handlebars); identity comes from reference images.
# grid_x = columns, grid_y = rows. Stage 4 "3x2" = 3 columns × 2 rows.

_STAGE_1_PROMPT = """Using the attached reference image(s) as the ground-truth facial identity — they define the exact face; override any conflicting detail — generate ONE high-resolution image containing a clean 4x4 grid (16 tiles) of the SAME person's head and face, close-up.

GRID FORMAT (critical): a seamless 4x4 grid of 16 equal-size square tiles, each filled edge-to-edge with its own photo. NO borders, gutters, gaps, frames, lines, numbers, labels, captions, or text anywhere. All 16 tiles identical in size. This image will be sliced into 16 separate training crops, so every tile must be a complete standalone photo.

ROW 1 — ONE FULL HEAD ROTATION (left to right): (1) straight-on frontal, (2) 3/4 turn, (3) full side profile, (4) back-of-head / rear 3/4. Same neutral expression and lighting family so row 1 reads as one continuous rotation of the same head.

ROWS 2–4 — ANGLE, ELEVATION & LIGHTING VARIETY: rotate through frontal, both 3/4 views, both side profiles, gentle overhead (5–15° above), gentle low (5–15° below), and 1–2 dramatic high/low angles (30–45°). Vary head tilt and eye contact. Different lighting mood per tile: soft daylight, warm golden hour, neutral studio key, Rembrandt/side light, rim/backlight.

FRAMING DISTANCE (distribute; face fills 60–90% of every tile): ~5 extreme macro (85–100%, pores/eyelashes/iris sharp), ~7 standard close-up (60–75%), ~4 looser head-and-shoulders (45–60%). Pair each angle with a different distance where possible.

LOCK (identical in every tile): facial bone structure, eye shape/color, nose, lips, skin tone, ALL skin characteristics (freckles, moles, scars, pores, texture); hair color, strand texture, shine, thickness, length.

VARY only: camera angle/elevation, framing distance, lighting, head tilt, hairstyle ARRANGEMENT (down, waves, half-up, low bun, high ponytail, side braid, tucked, center/side part, windblown) — never hair color or length.

EXPRESSION: neutral/calm across all tiles. Background: simple neutral studio (white/light gray/soft beige).

QUALITY: photorealistic, sharp, maximum skin-texture detail, natural color. Instantly recognizable as the SAME individual in all 16 tiles. No text, watermarks, logos, borders, or numbering. Render at maximum available resolution."""

_STAGE_2_PROMPT = """You are given reference images in priority order. PRIMARY IDENTITY REFERENCES (highest priority — define ground-truth facial identity; override any conflicting detail): the character's seed face reference(s). SECONDARY SUPPORTING REFERENCES (use only to reinforce angle/lighting/hairstyle range; if any conflicts with the primary references, DEFER to the primary references): the previous identity collage. Using the PRIMARY references to lock identity, generate ONE high-resolution image containing a clean 4x4 grid (16 tiles) of the SAME person.

GRID FORMAT (critical): a seamless 4x4 grid of 16 equal-size square tiles filled edge-to-edge; NO borders, gutters, gaps, frames, lines, numbers, labels, captions, or text; identical tile size; sliced into 16 standalone crops.

EXPRESSIONS & EMOTIONS (8 tiles): laughing/joyful; shocked/surprised; pouty/sad; angry/intense; confused/thinking; smug/playful smirk; sleepy/relaxed; mysterious/subtle. PLAYFUL POSES & UNUSUAL ANGLES (8 tiles): tongue out; winking; blowing a kiss; head tilted/twisted; over-the-shoulder glance; cheek puffed; plus two expressive candids. Photograph each from a DIFFERENT camera elevation.

FACE SIZE: 60–75% of every tile (a few tighter beauty-macros allowed). Mix focal lengths, vary depth of field, 1–2 subtle dutch tilts.

LOCK (identical): facial bone structure, eye shape/color, nose, lips, skin tone, ALL skin characteristics; hair color, texture, shine, thickness, length. VARY only: expression, pose, camera angle/elevation, lens/framing, lighting, hairstyle ARRANGEMENT.

LIGHTING/BACKGROUND: professional even lighting; simple neutral background lightly varied per tile. QUALITY: photorealistic, sharp, high skin detail. Instantly recognizable as the SAME person in all 16 tiles. No text/watermark/logo/border/numbering. Maximum resolution."""

_STAGE_3_PROMPT = """You are given reference images in priority order. PRIMARY IDENTITY REFERENCES (highest priority; define the exact face; override conflicts): the character's seed face reference(s). SECONDARY SUPPORTING REFERENCES (reinforce range only; defer to primary on conflict): the previous identity collage. Using the PRIMARY references to lock identity, generate ONE high-resolution image containing a clean 4x4 grid (16 tiles) of the SAME person from the chest/shoulders up, fully clothed in simple everyday outfits (crewneck tee, casual blouse, light sweater, denim/utility jacket, turtleneck) — relaxed natural fit.

GRID FORMAT (critical): a seamless 4x4 grid of 16 equal-size square tiles filled edge-to-edge; NO borders, gutters, gaps, frames, lines, numbers, labels, captions, or text; identical tile size; sliced into 16 standalone crops.

ANGLES: frontal; left/right 3/4; left/right profile; high angle (above); low angle (below); slight over-the-shoulder both directions. ARM & HAND VARIATION: arms relaxed; arms crossed casually; hand near face/hair; hands clasped; hand adjusting collar/hair.

WARDROBE & BACKGROUND: EACH tile a DIFFERENT casual outfit AND a DIFFERENT background (plain studio in varying colors, soft outdoor bokeh — park/street/cafe, simple indoor rooms) — never reuse one backdrop (a constant background gets wrongly baked into identity).

FRAMING: mix tighter head-and-shoulders with looser chest-up; face clearly visible and sharp in every tile (≥ ~35% of frame). Vary camera height, depth of field, offset.

LOCK (identical): facial bone structure, eye shape/color, nose, lips, skin tone, ALL skin characteristics; hair color, texture, shine, thickness, length. Natural realistic proportions. VARY only: pose, arm/hand position, camera angle/elevation, lens/framing, outfit, background, lighting, hairstyle ARRANGEMENT.

QUALITY: photorealistic, sharp, face in crisp focus. Instantly recognizable as the SAME person in all 16 tiles. No text/watermark/logo/border/numbering. Maximum resolution."""

_STAGE_4_PROMPT = """You are given reference images in priority order. PRIMARY IDENTITY REFERENCES (highest priority; define the exact face; override conflicts; keep the face sharp even at a distance): the character's seed face reference(s). SECONDARY SUPPORTING REFERENCES (reinforce range only; defer to primary on conflict): the previous identity collage. Using the PRIMARY references to lock identity, generate ONE high-resolution PORTRAIT image containing a clean 3x2 grid (3 columns, 2 rows = 6 tall vertical tiles) of the SAME person, fully clothed in simple everyday/casual outfits, across varied real-world settings.

GRID FORMAT (critical): a seamless 3x2 grid of 6 equal-size VERTICAL (portrait) tiles, each filled edge-to-edge with its own photo. NO borders, gutters, gaps, frames, lines, numbers, labels, captions, or text. Identical tile size. Sliced into 6 standalone crops, so every tile is one complete standing/seated photo.

FRAMING DISTRIBUTION (bias toward larger faces — critical for identity): ~4 of 6 tiles are THREE-QUARTER / MEDIUM body (thigh-up or knee-up, subject filling most of the tall frame so the face stays large and sharp); ~2 tiles are true FULL-LENGTH head-to-toe. Do NOT make every tile a small full-length figure.

SETTINGS (one distinct per tile): outdoor urban street; indoor cafe; park/nature path; home living room; office/study; plain neutral studio backdrop (one anchor tile). POSE & CAMERA: standing straight-on; walking mid-stride; seated; 3/4 turned; candid (leaning, hands in pockets, adjusting bag/hair). Vary camera height.

CRITICAL — FACE DETAIL AT DISTANCE: even in full-length tiles the face stays sharp, clearly resolved, recognizable — not soft/blurry. Render the face at higher effective detail than the surroundings if needed; background may be softly blurred, face stays crisp.

WARDROBE: simple, comfortable, fully-clothed everyday outfits per setting (jeans + tee, casual dress, light jacket, sneakers) — relaxed natural fit. LOCK (identical): facial identity + ALL skin characteristics; hair color/texture/shine/thickness/length; natural realistic body build and proportions. VARY only: setting/background, full-body pose, camera height/distance (within the distribution above), outfit, lighting, hairstyle ARRANGEMENT.

QUALITY: photorealistic, sharp, face in crisp focus in every tile. Instantly recognizable as the SAME person. No text/watermark/logo/border/numbering. Maximum resolution."""


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
        "label": "Full-body Lifestyle (portrait)",
        "prompt": _STAGE_4_PROMPT,
        "width": 2160,
        "height": 3840,
        "grid_x": 3,
        "grid_y": 2,
        "inset_pct": 0.015,
        "reference_policy": "collage_1",
    },
]


def upgrade() -> None:
    op.add_column(
        "dataset_templates",
        sa.Column(
            "collage_stages",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.add_column(
        "dataset_templates",
        sa.Column("seed_reference_media_id", sa.Text(), nullable=True),
    )

    # Seed the recipe onto BOTH seeded system rows (sdxl + z-image). The recipe
    # collage prompts are identical for both, since model_target only affects
    # caption/train, not collage generation. Bind the JSON text and CAST to jsonb
    # so a valid JSONB array is written (avoids double-encoding the payload).
    op.execute(
        sa.text(
            "UPDATE dataset_templates "
            "SET collage_stages = CAST(:stages AS jsonb) "
            "WHERE user_id IS NULL"
        ).bindparams(sa.bindparam("stages", value=json.dumps(COLLAGE_STAGES)))
    )


def downgrade() -> None:
    op.drop_column("dataset_templates", "seed_reference_media_id")
    op.drop_column("dataset_templates", "collage_stages")
