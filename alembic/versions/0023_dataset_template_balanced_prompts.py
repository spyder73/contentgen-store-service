"""dataset_template collage_stages: balanced left/right + elevation prompts

Reason: the 0022-seeded stage prompts said "both left and right" but did not
ENFORCE it, so the image model defaulted to left-turned faces (~10:1 left bias,
almost no right-side coverage, no high/low camera angles) — which corrupts a
LoRA (it never learns the right side of the face). This rewrites each stage's
prompt to MANDATE mirrored left/right coverage with explicit counts and to force
high- and low-angle tiles. Geometry (size/grid/inset/reference_policy) is
unchanged from 0022; only the prompt text changes.

Data-content update only (no schema change). Applies to the seeded SYSTEM rows
(user_id IS NULL); existing per-dataset private clones are untouched — edit those
via the Settings stage editor.

revision: 0023
down_revision: 0022
"""
from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None

# grid_x = columns, grid_y = rows. Stage 4 "3x2" = 3 columns x 2 rows.
COLLAGE_STAGES = [
    {
        "label": "Identity — Face Rotation",
        "prompt": """Using the attached reference image(s) as the ground-truth facial identity — they define the exact face; override any conflicting detail — generate ONE high-resolution image containing a clean 4x4 grid (16 tiles) of the SAME person's head and face, close-up.

GRID FORMAT (critical): a seamless 4x4 grid of 16 equal-size square tiles, each filled edge-to-edge with its own photo. NO borders, gutters, gaps, frames, lines, numbers, labels, captions, or text anywhere. All 16 tiles identical in size. This image is sliced into 16 standalone training crops, so every tile must be a complete standalone photo.

MANDATORY LEFT/RIGHT SYMMETRY — the single most important rule. The head must turn to the character's RIGHT in exactly as many tiles as it turns to her LEFT. Do NOT default to left-turned faces; a grid that shows mostly one side is a FAILURE. Mirror every left-turn with an equivalent right-turn. Across the 16 tiles include ALL of:
- 1 straight-on frontal.
- 2 three-quarter views turned to her LEFT and 2 three-quarter views turned to her RIGHT (viewer sees the corresponding cheek).
- 1 full LEFT profile (90 degrees, face pointing to frame-left) and 1 full RIGHT profile (90 degrees, face pointing to frame-right).
- 1 rear three-quarter over her LEFT shoulder and 1 rear three-quarter over her RIGHT shoulder.
- 1 straight back-of-head (rear view).
- 2 HIGH-angle tiles (camera clearly ABOVE the head, looking DOWN at the face) — one frontal, one turned.
- 2 LOW-angle tiles (camera clearly BELOW the chin, looking UP at the face) — one frontal, one turned.
- Fill any remaining tiles with additional near-frontal views at a different framing distance.

FRAMING DISTANCE (vary across the 16 tiles; face fills 60-90% of each): about 4 extreme macro close-ups (face 85-100%, pores/eyelashes/iris sharp), about 8 standard close-ups (face 60-75%), about 4 looser head-and-shoulders (face 45-60%).

LIGHTING (vary per tile): soft daylight, warm golden hour, neutral studio key, dramatic Rembrandt/side light, rim/backlight.

LOCK (identical in every tile): facial bone structure, eye shape/color, nose, lips, skin tone, ALL skin characteristics (freckles, moles, scars, pores, texture); hair color, strand texture, shine, thickness, length.

VARY only: which way the head turns (balanced left/right as above), camera elevation, framing distance, lighting, head tilt, hairstyle ARRANGEMENT (down, soft waves, half-up, low bun, high ponytail, side braid, tucked behind one ear, center/side part, windblown) — never hair color or length.

EXPRESSION: neutral/calm across all tiles. Background: simple neutral studio (white, light gray, or soft beige).

QUALITY: photorealistic, sharp, maximum skin-texture detail. Instantly recognizable as the SAME individual in all 16 tiles. No text, watermarks, logos, borders, or numbering. Render at maximum available resolution.""",
        "width": 2048,
        "height": 2048,
        "grid_x": 4,
        "grid_y": 4,
        "inset_pct": 0.015,
        "reference_policy": "identity_seed",
    },
    {
        "label": "Expressions & Emotions",
        "prompt": """You are given reference images in PRIORITY ORDER:
- PRIMARY IDENTITY REFERENCES (highest priority — define the ground-truth facial identity; override any conflicting detail): the character's seed face reference image(s).
- SECONDARY SUPPORTING REFERENCES (use ONLY to reinforce the angle/lighting/hairstyle range already established; if any conflicts with the PRIMARY references, DEFER to the PRIMARY references): the previous collage(s) from earlier stages.

Using the PRIMARY references to lock facial identity and the SECONDARY references only as supporting context, generate ONE high-resolution image containing a clean 4x4 grid (16 tiles) of the SAME person.

GRID FORMAT (critical): a seamless 4x4 grid of 16 equal-size square tiles, each filled edge-to-edge; NO borders, gutters, gaps, frames, lines, numbers, labels, captions, or text anywhere; identical tile size; sliced into 16 standalone crops.

EXPRESSIONS & EMOTIONS (8 tiles): laughing/joyful; shocked/surprised; pouty/sad; angry/intense; confused/thinking; smug/playful smirk; sleepy/relaxed; mysterious/subtle. PLAYFUL POSES (8 tiles): tongue out; winking; blowing a kiss; head tilted; over-the-shoulder glance; cheek puffed; plus two expressive candids.

MANDATORY BALANCED ORIENTATION — do NOT hold every expression at a left-turned angle (the previous version was left-biased). Distribute the expressions EVENLY across head orientations: roughly one third with the head turned to her LEFT, one third turned to her RIGHT, one third frontal — with the LEFT count and the RIGHT count equal. Also include at least 2 tiles shot from a HIGH angle (camera above, looking down) and at least 2 from a LOW angle (camera below, looking up).

FACE SIZE: face fills 60-75% of every tile (a few tighter beauty-macros allowed). Mix focal lengths, vary depth of field, include 1-2 subtle dutch tilts.

LOCK (identical): facial bone structure, eye shape/color, nose, lips, skin tone, ALL skin characteristics; hair color, texture, shine, thickness, length. VARY only: expression/emotion, pose, head orientation (balanced left/right), camera elevation, lens/framing, lighting, hairstyle ARRANGEMENT (never hair color or length).

LIGHTING/BACKGROUND: professional even lighting; simple neutral background, lightly varied per tile. QUALITY: photorealistic, sharp, high skin detail. Instantly recognizable as the SAME person in all 16 tiles. No text/watermark/logo/border/numbering. Render at maximum available resolution.""",
        "width": 2048,
        "height": 2048,
        "grid_x": 4,
        "grid_y": 4,
        "inset_pct": 0.015,
        "reference_policy": "collage_1",
    },
    {
        "label": "Angles · Upper-body · Wardrobe · Backgrounds",
        "prompt": """You are given reference images in PRIORITY ORDER:
- PRIMARY IDENTITY REFERENCES (highest priority — define the exact face; override conflicts): the character's seed face reference image(s).
- SECONDARY SUPPORTING REFERENCES (reinforce range only; defer to PRIMARY on conflict): the previous collage(s) from earlier stages.

Using the PRIMARY references to lock facial identity and the SECONDARY references only as supporting context, generate ONE high-resolution image containing a clean 4x4 grid (16 tiles) of the SAME person shown from the chest/shoulders up, fully clothed in simple everyday outfits (crewneck tee, casual blouse, light sweater, denim/utility jacket, turtleneck) — relaxed natural fit, nothing tight or body-emphasizing.

GRID FORMAT (critical): a seamless 4x4 grid of 16 equal-size square tiles, filled edge-to-edge; NO borders, gutters, gaps, frames, lines, numbers, labels, captions, or text; identical tile size; sliced into 16 standalone crops.

MANDATORY LEFT/RIGHT SYMMETRY — do NOT default to left-facing (the previous version was heavily left-biased). The body and head must turn to her RIGHT in as many tiles as to her LEFT. Include, balanced: frontal straight-on; equal numbers of 3/4 turned LEFT and 3/4 turned RIGHT; a full LEFT profile AND a full RIGHT profile; a rear 3/4 over her LEFT shoulder AND over her RIGHT shoulder; at least 2 HIGH-angle tiles (camera above, looking down) and at least 2 LOW-angle tiles (camera below, looking up).

ARM & HAND VARIATION: arms relaxed at sides; arms crossed casually; one hand near face/hair; hands clasped; one hand adjusting collar or hair.

WARDROBE & BACKGROUND: give EACH tile a DIFFERENT casual outfit AND a DIFFERENT background (plain studio in varying colors, soft outdoor bokeh — park/street/cafe, simple indoor rooms) — never reuse one backdrop.

FRAMING: mix tighter head-and-shoulders with looser chest-up crops; face clearly visible and sharp in every tile (face at least ~35% of the frame).

LOCK (identical): facial bone structure, eye shape/color, nose, lips, skin tone, ALL skin characteristics; hair color, texture, shine, thickness, length. Natural realistic proportions. VARY only: pose, head/body orientation (balanced left/right), arm/hand position, camera elevation, lens/framing, outfit, background, lighting, hairstyle ARRANGEMENT.

QUALITY: photorealistic, sharp, face in crisp focus. Instantly recognizable as the SAME person in all 16 tiles. No text/watermark/logo/border/numbering. Render at maximum available resolution.""",
        "width": 2048,
        "height": 2048,
        "grid_x": 4,
        "grid_y": 4,
        "inset_pct": 0.015,
        "reference_policy": "collage_1",
    },
    {
        "label": "Full-body Lifestyle (portrait)",
        "prompt": """You are given reference images in PRIORITY ORDER:
- PRIMARY IDENTITY REFERENCES (highest priority — define the exact face; override conflicts; keep the face sharp and recognizable even at a distance): the character's seed face reference image(s).
- SECONDARY SUPPORTING REFERENCES (reinforce wardrobe/angle/lighting range; defer to PRIMARY on conflict): the previous collage(s) from earlier stages.

Using the PRIMARY references to lock facial identity and the SECONDARY references only as supporting context, generate ONE high-resolution PORTRAIT image containing a clean 3x2 grid (3 columns, 2 rows = 6 tall vertical tiles) of the SAME person, fully clothed in simple everyday/casual outfits, across varied real-world settings.

GRID FORMAT (critical): a seamless 3x2 grid of 6 equal-size VERTICAL (portrait) tiles, each filled edge-to-edge with its own photo. NO borders, gutters, gaps, frames, lines, numbers, labels, captions, or text anywhere. Identical tile size. Sliced into 6 standalone crops, so every tile is one complete standing/seated photo.

FRAMING DISTRIBUTION (bias toward larger faces — critical for identity): about 4 of the 6 tiles are THREE-QUARTER / MEDIUM body (thigh-up or knee-up, subject filling most of the tall frame so the face stays large and sharp); about 2 tiles are true FULL-LENGTH head-to-toe. Do NOT make every tile a small full-length figure.

MANDATORY BALANCED ORIENTATION — do NOT default to left-facing. Across the 6 tiles include at least 2 with the body/face oriented to her RIGHT and at least 2 to her LEFT, plus one frontal and one rear/over-the-shoulder view. Vary camera height (eye-level, slightly low looking up, slightly elevated looking down).

SETTINGS (one distinct per tile): outdoor urban street; indoor cafe; park/nature path; home living room; office/study; plain neutral studio backdrop (one anchor tile). POSE: standing straight-on; walking mid-stride; seated; 3/4 turned; candid (leaning, hands in pockets, adjusting bag/hair).

CRITICAL — FACE DETAIL AT DISTANCE: even in full-length tiles the face must stay sharp, clearly resolved, and recognizable — not soft or blurry. Render the face at higher effective detail than the surroundings if needed; background may be softly blurred, the face must stay in crisp focus.

WARDROBE: simple, comfortable, fully-clothed everyday outfits per setting (jeans + tee, casual dress, light jacket, sneakers) — relaxed natural fit. LOCK (identical): facial identity and ALL skin characteristics; hair color/texture/shine/thickness/length; natural realistic body build and proportions. VARY only: setting/background, full-body pose, body/face orientation (balanced left/right), camera height/distance (within the framing distribution), outfit, lighting, hairstyle ARRANGEMENT.

QUALITY: photorealistic, sharp, face in crisp focus in every tile. Instantly recognizable as the SAME person. No text/watermark/logo/border/numbering. Render at maximum available resolution.""",
        "width": 2160,
        "height": 3840,
        "grid_x": 3,
        "grid_y": 2,
        "inset_pct": 0.015,
        "reference_policy": "collage_1",
    },
]


def upgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE dataset_templates SET collage_stages = CAST(:stages AS jsonb) "
            "WHERE user_id IS NULL"
        ).bindparams(sa.bindparam("stages", value=json.dumps(COLLAGE_STAGES)))
    )


def downgrade() -> None:
    # Data-content-only migration (no schema change). The prior 0022 prompt text
    # is superseded; downgrade is a no-op rather than restoring the left-biased
    # prompts. The column itself is dropped by 0022's downgrade if needed.
    pass
