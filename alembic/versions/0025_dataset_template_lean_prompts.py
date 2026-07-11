"""dataset_template collage_stages: lean prompts, positional mirrored-pair orientation

Reason: the 0024 stage prompts were saturated with pipeline meta-language the
image model cannot act on — "PRIMARY IDENTITY REFERENCES"/"SECONDARY SUPPORTING
REFERENCES" and "PRIORITY ORDER" framing describe how the ORCHESTRATOR should
weight input images, not something the model can render; "the previous version
was left-biased" is a note to a human about an earlier migration, not an
instruction. Worse, the "MANDATORY LEFT/RIGHT SYMMETRY" wording relied on
negation-heavy orientation counting ("do NOT default to left-turned faces",
"must turn to her RIGHT in exactly as many tiles as ... LEFT") which live
tests on 2026-07-11 (two independent 16-tile collage runs against the 0024
recipe) showed the image model still largely ignores: both runs produced
roughly 1 right-facing tile out of 16, i.e. still ~15:1 left-biased despite
the "mandatory" language.

0025 rewrites every stage prompt to:
  * drop all pipeline/meta language (reference-priority framing, "previous
    version" callouts) and keep only content the model can directly act on;
  * replace negation-heavy balance mandates with POSITIONAL, mirrored-pair
    instructions anchored to the frame edges (e.g. "nose pointing at the
    RIGHT frame edge" / "toward the LEFT edge"), always leading with the
    right-facing tile so the model isn't primed to default left again.

Geometry (size/grid/inset/reference_policy) and stage labels are byte-for-byte
unchanged from 0024; only prompt text changes.

Data-content update only (no schema change). Applies to the seeded SYSTEM rows
(user_id IS NULL); existing per-dataset private clones are intentionally left
untouched (they keep whatever recipe they were cloned with — edit those via
the Settings stage editor). New datasets created after this migration pick up
the new prompts.

revision: 0025
down_revision: 0024
"""
from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None

# grid_x = columns, grid_y = rows. Stages 4-5 "3x2" = 3 columns x 2 rows.
COLLAGE_STAGES = [
    {
        "label": "Identity — Face Rotation",
        "prompt": """The reference images show one woman — match her face, hair, and skin exactly in every tile.

Create ONE photo: a seamless 4x4 grid of 16 equal square tiles, each a complete standalone close-up photo of this same woman, filled edge-to-edge — no borders, gutters, text, or logos anywhere (the image is sliced into 16 separate training photos).

Tile plan, row by row (left to right) — head directions are anchored to the frame edges:
- Row 1: full profile, nose pointing at the RIGHT frame edge; three-quarter view toward the RIGHT edge; straight-on frontal; three-quarter view toward the LEFT edge.
- Row 2: full profile toward the LEFT edge; rear three-quarter over her right shoulder; straight back of head; rear three-quarter over her left shoulder.
- Row 3: HIGH camera looking down, frontal; HIGH camera looking down, head turned toward the RIGHT edge; LOW camera looking up, frontal; LOW camera looking up, head turned toward the LEFT edge.
- Row 4: extreme macro (face fills 90%+), frontal; extreme macro, turned toward the RIGHT edge; head-and-shoulders, frontal; head-and-shoulders, turned toward the LEFT edge.

Turned poses come in mirrored pairs — exactly as many toward the right edge as toward the left edge.

Framing: mix extreme macro (face 85-100%), standard close-up (60-75%), and head-and-shoulders (45-60%) as assigned above. Lighting varies per tile: soft daylight, golden hour, studio key, Rembrandt side light, rim light. Hairstyle arrangement varies (down, half-up, bun, ponytail, braid, tucked behind ear); hair color and length never change. Expression neutral and calm. Background: plain neutral studio.

Photorealistic, sharp, maximum skin detail, the same woman instantly recognizable in all 16 tiles.""",
        "width": 2048,
        "height": 2048,
        "grid_x": 4,
        "grid_y": 4,
        "inset_pct": 0.015,
        "reference_policy": "identity_seed",
    },
    {
        "label": "Expressions & Emotions",
        "prompt": """Every reference image shows the same woman — the grid reference shows her from multiple angles. Match her face, hair, and skin exactly.

Create ONE photo: a seamless 4x4 grid of 16 equal square tiles, each a complete standalone photo, filled edge-to-edge — no borders, gutters, text, or logos anywhere (the image is sliced into 16 separate training photos).

One expression per tile: laughing; shocked/surprised; pouty/sad; angry/intense; confused/thinking; smug playful smirk; sleepy/relaxed; mysterious/subtle; tongue out; winking; blowing a kiss; head tilted; over-the-shoulder glance; cheeks puffed; two expressive candids.

Head direction rotates tile to tile in mirrored pairs — exactly as many heads turned toward the RIGHT frame edge as toward the left edge, the rest frontal; include 2 high-angle tiles (camera above) and 2 low-angle tiles (camera below).

Face fills 60-75% of each tile (a couple of tighter beauty macros allowed); vary lens and depth of field, 1-2 subtle dutch tilts. Even professional lighting; lightly varied neutral backgrounds. Hairstyle arrangement may vary; hair color and length never change.

Photorealistic, sharp, high skin detail, the same woman instantly recognizable in all 16 tiles.""",
        "width": 2048,
        "height": 2048,
        "grid_x": 4,
        "grid_y": 4,
        "inset_pct": 0.015,
        "reference_policy": "collage_1",
    },
    {
        "label": "Angles · Upper-body · Wardrobe · Backgrounds",
        "prompt": """Every reference image shows the same woman. Match her face, hair, and skin exactly.

Create ONE photo: a seamless 4x4 grid of 16 equal square tiles, each a complete standalone photo of her from the chest/shoulders up, filled edge-to-edge — no borders, gutters, text, or logos anywhere (the image is sliced into 16 separate training photos).

Body and head directions in mirrored pairs anchored to the frame edges: equal counts of three-quarter turns toward the RIGHT edge and toward the left edge; one full profile facing the RIGHT edge and one facing the left edge; one rear three-quarter over each shoulder; the rest frontal. Include 2 high-angle and 2 low-angle tiles.

EACH tile: a DIFFERENT simple everyday outfit (crewneck tee, blouse, light sweater, denim or utility jacket, turtleneck — relaxed natural fit) AND a DIFFERENT background (plain studio in varied colors; outdoor bokeh — park, street, cafe; simple indoor rooms). Arms vary: relaxed at sides, crossed, one hand near face or hair, hands clasped, adjusting collar.

Framing mixes tight head-and-shoulders with looser chest-up; the face stays sharp and at least ~35% of every frame. Hairstyle arrangement varies; hair color and length never change.

Photorealistic, sharp, the same woman instantly recognizable in all 16 tiles.""",
        "width": 2048,
        "height": 2048,
        "grid_x": 4,
        "grid_y": 4,
        "inset_pct": 0.015,
        "reference_policy": "collage_1",
    },
    {
        "label": "Full-body Outfit Styles (portrait)",
        "prompt": """Every reference image shows the same woman. Match her face, hair, skin, and natural body proportions exactly; keep the face sharp and recognizable even at a distance.

Create ONE tall PORTRAIT photo: a seamless 3x2 grid (3 columns, 2 rows) of 6 equal vertical tiles, each one complete standalone photo, filled edge-to-edge — no borders, gutters, text, or logos anywhere (the image is sliced into 6 separate training photos).

Framing: 4 tiles three-quarter/medium body (thigh-up or knee-up — she fills the tall frame, face large and sharp); 2 tiles true full-length head-to-toe.

One outfit style per tile, never repeated: (1) classy formal — tailored blazer or cocktail dress, upscale interior; (2) business casual — blouse and slacks, office; (3) comfy casual — oversized knit and leggings, home sofa; (4) streetwear — hoodie, jeans, sneakers, urban street; (5) sporty athleisure — leggings and zip jacket, park; (6) summer casual — sundress, cafe terrace. All fully clothed, relaxed natural fit.

Poses in mirrored pairs: at least two tiles with body and gaze toward the RIGHT frame edge and matching tiles toward the left edge, plus one frontal and one rear over-the-shoulder view. Vary camera height (eye level; low looking up; elevated looking down). Poses: standing, walking mid-stride, seated, leaning, hands in pockets, adjusting bag or hair.

Background softly blurred; the face in crisp focus in every tile, rendered at higher detail than the surroundings.

Photorealistic, sharp, the same woman instantly recognizable in all 6 tiles.""",
        "width": 2160,
        "height": 3840,
        "grid_x": 3,
        "grid_y": 2,
        "inset_pct": 0.015,
        "reference_policy": "collage_1",
    },
    {
        "label": "Full-body Activities (portrait)",
        "prompt": """Every reference image shows the same woman. Match her face, hair, skin, and natural body proportions exactly; keep the face sharp and recognizable even at a distance.

Create ONE tall PORTRAIT photo: a seamless 3x2 grid (3 columns, 2 rows) of 6 equal vertical tiles, each one complete standalone photo, filled edge-to-edge — no borders, gutters, text, or logos anywhere (the image is sliced into 6 separate training photos).

Framing: 4 tiles three-quarter/medium body (thigh-up or knee-up, face large and sharp); 2 tiles true full-length head-to-toe.

One activity per tile, never repeated: (1) walking mid-stride at a crosswalk, streetwear; (2) jogging or a post-run stretch outdoors, sportswear; (3) a yoga pose on a mat, athletic wear; (4) seated at a cafe with coffee or a book, smart casual; (5) browsing a shop or market, casual chic; (6) cooking or tending plants at home, comfy loungewear. Movement and hands natural and anatomically plausible. All fully clothed, relaxed natural fit.

Poses in mirrored pairs: at least two tiles oriented toward the RIGHT frame edge and matching tiles toward the left edge, plus one frontal and one rear over-the-shoulder view. Vary camera height.

Background softly blurred; the face in crisp focus in every tile.

Photorealistic, sharp, the same woman instantly recognizable in all 6 tiles.""",
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
    # Data-content-only migration (no schema change). The prior 0024 prompt
    # text is superseded; downgrade is a no-op rather than restoring the
    # meta-language-heavy, negation-based orientation prompts. The column
    # itself is dropped by 0022's downgrade if needed.
    pass
