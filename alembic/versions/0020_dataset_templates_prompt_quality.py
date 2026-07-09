"""dataset templates prompt quality

Revision ID: 0020
Revises: 0019
Create Date: 2026-07-09

Migration 0019 seeded a system default `dataset_templates` row whose
`collage_prompt` contains Handlebars templating (`{{#each ...}}`,
`{{@index + 1}}`, `{{#if ...}}`) that the Go backend does NOT render — it is
sent LITERALLY to the image model. It also instructs a neutral/constant
background and expression across all 16 tiles, which is bad for LoRA
dataset variety (a constant background/expression gets baked into the
character identity).

This migration:
  1. Updates the existing default row's `collage_prompt` to a plain-text,
     high-variety prompt (no templating syntax).
  2. Inserts a second system template tuned for Z-Image (ZIT) training with
     a natural-language `caption_format`.

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0020"
down_revision: Union[str, None] = "0019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

IMPROVED_COLLAGE_PROMPT = """Using the attached reference image(s) as the ground-truth facial identity (they define the exact face — override any conflicting detail), generate ONE high-resolution image containing a clean 4x4 grid (16 tiles) of the SAME person.

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


def upgrade() -> None:
    op.execute(
        f"""
        UPDATE dataset_templates
        SET collage_prompt = '{IMPROVED_COLLAGE_PROMPT.replace("'", "''")}'
        WHERE name = 'Identity Collage (16-tile)' AND user_id IS NULL
        """
    )

    op.execute(
        f"""
        INSERT INTO dataset_templates (
            name, description, collage_prompt, collage_model, collage_width, collage_height,
            collage_quality, split_grid_x, split_grid_y, upscale_enabled, upscale_model,
            target_megapixels, upscale_enhance_details, upscale_realism,
            caption_vision_model, caption_format, is_default, user_id
        ) VALUES (
            'Identity Collage (16-tile) — Z-Image',
            'Z-Image / natural-language captioning preset',
            '{IMPROVED_COLLAGE_PROMPT.replace("'", "''")}',
            'openai:gpt-image@2',
            3840,
            2160,
            'high',
            4,
            4,
            TRUE,
            'prunaai:p-image@upscale',
            4,
            FALSE,
            FALSE,
            'google/gemini-2.5-flash',
            'A photo of {{trigger_token}}, {{description}}.',
            FALSE,
            NULL
        )
        """
    )


def downgrade() -> None:
    # Simple/no-op-ish downgrade: only remove the inserted Z-Image row. The
    # default row's collage_prompt is intentionally left improved (not
    # reverted to the old Handlebars text) to keep this migration simple.
    op.execute(
        """
        DELETE FROM dataset_templates
        WHERE name = 'Identity Collage (16-tile) — Z-Image' AND user_id IS NULL
        """
    )
