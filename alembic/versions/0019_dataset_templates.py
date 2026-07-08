"""dataset templates

Revision ID: 0019
Revises: 0018
Create Date: 2026-07-08

Dataset templates store reusable collage generation prompts with all settings
for collage generation, splitting, upscaling, and captioning.

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0019"
down_revision: Union[str, None] = "0018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DEFAULT_COLLAGE_PROMPT = """You are given the following reference images, in priority order:

PRIMARY IDENTITY REFERENCES (highest priority — these define ground-truth facial identity, override any conflicting detail):
{{#each reference_images}}
{{@index + 1}}. {{this.description}}
{{/each}}

{{#if previous_collages}}
SECONDARY SUPPORTING REFERENCES (use only to reinforce angle range, lighting style, and hairstyle variety already established):
{{#each previous_collages}}
{{@index + 4}}. Previous collage {{@index + 1}}
{{/each}}
{{/if}}

Using the PRIMARY references to lock facial identity, and the SECONDARY references only as supporting context, create a high-resolution character identity collage.

Create a grid of 16 varied compositions of this SAME CHARACTER'S face:

FACIAL ANGLES & PERSPECTIVES (varied coverage):
- Frontal straight-on view
- 3/4 views (both left and right)
- Side profiles (both left and right)
- Back 3/4 views (both left and right)
- Overhead/tilted down angles (5-15° camera tilt downward)
- Underhead/tilted up angles (5-15° camera tilt upward)
- Extreme low camera angles (photographed from 30-45° below)
- Extreme high camera angles (photographed from 30-45° above)

FRAMING & DISTANCE DISTRIBUTION (mandatory — distribute across the 16 tiles):
- 4-5 tiles: EXTREME MACRO close-up — face fills 90-100% of frame
- 6-7 tiles: STANDARD close-up headshot — face fills 60-75% of frame
- 4-5 tiles: LOOSER crop — face fills 35-50% of frame

LENS & COMPOSITION VARIETY:
- Mix focal lengths: macro/beauty shots, 85mm portrait, 35mm wider framing
- Vary framing: centered, off-center, negative space
- Include 1-2 tiles with dutch tilt (5-10° camera roll)
- Vary depth of field: sharp foreground-to-background, soft blurred background

HAIR — IDENTITY VS STYLING:
- LOCKED: hair color, texture, shine, thickness, length
- VARIED: hairstyle arrangement — straight down, waves, half-up, bun, ponytail, braid, etc.

LIGHTING VARIATIONS:
- Soft warm lighting (golden hour, studio key)
- Cool daylight (neutral, professional)
- Dramatic side-lighting (Rembrandt, split)
- Overhead/downward lighting
- Rim-lighting (backlit edge definition)

EXPRESSION & DETAIL:
- Neutral or calm expression (not smiling, not animated)
- Professional studio setting, even lighting
- Neutral background (white, light gray, beige)
- Maximum skin detail — pores, texture, fine details

Requirements:
- Maintain consistent facial identity across all tiles
- Preserve all skin characteristics: texture, freckles, moles, scars, pores
- Preserve hair color, texture, thickness, length exactly — only styling varies
- Vary angle, camera position, lens/framing, lighting, hairstyle, head position
- Arrange as clean 4x4 grid with consistent tile sizing
- Render at maximum resolution available (8K preferred)

This collage locks in facial identity consistency for LoRA training."""


def upgrade() -> None:
    op.create_table(
        "dataset_templates",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=False),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("collage_prompt", sa.Text(), nullable=False),
        sa.Column(
            "collage_model",
            sa.Text(),
            nullable=False,
            server_default="openai:gpt-image@2",
        ),
        sa.Column(
            "collage_width",
            sa.Integer(),
            nullable=False,
            server_default="3840",
        ),
        sa.Column(
            "collage_height",
            sa.Integer(),
            nullable=False,
            server_default="2160",
        ),
        sa.Column("collage_quality", sa.Text(), server_default="high"),
        sa.Column(
            "split_grid_x",
            sa.Integer(),
            nullable=False,
            server_default="4",
        ),
        sa.Column(
            "split_grid_y",
            sa.Integer(),
            nullable=False,
            server_default="4",
        ),
        sa.Column(
            "upscale_enabled",
            sa.Boolean(),
            nullable=False,
            server_default="TRUE",
        ),
        sa.Column("upscale_model", sa.Text(), server_default="prunaai:p-image@upscale"),
        sa.Column("target_megapixels", sa.Integer(), server_default="4"),
        sa.Column(
            "upscale_enhance_details",
            sa.Boolean(),
            server_default="FALSE",
        ),
        sa.Column(
            "upscale_realism",
            sa.Boolean(),
            server_default="FALSE",
        ),
        sa.Column(
            "caption_vision_model",
            sa.Text(),
            server_default="google/gemini-2.5-flash",
        ),
        sa.Column(
            "caption_format",
            sa.Text(),
            nullable=False,
            server_default="{{trigger_token}}, {{description}}",
        ),
        sa.Column(
            "is_default",
            sa.Boolean(),
            nullable=False,
            server_default="FALSE",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_dataset_templates_user_id",
        "dataset_templates",
        ["user_id"],
    )
    op.create_index(
        "ix_dataset_templates_is_default",
        "dataset_templates",
        ["is_default"],
        postgresql_where=sa.text("is_default = true"),
    )

    op.execute(
        f"""
        INSERT INTO dataset_templates (
            name, description, collage_prompt, collage_model, collage_width, collage_height,
            collage_quality, split_grid_x, split_grid_y, upscale_enabled, upscale_model,
            target_megapixels, upscale_enhance_details, upscale_realism,
            caption_vision_model, caption_format, is_default, user_id
        ) VALUES (
            'Identity Collage (16-tile)',
            'System default template for dataset generation with 4x4 grid collage',
            '{DEFAULT_COLLAGE_PROMPT.replace("'", "''")}',
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
            '{{trigger_token}}, {{description}}',
            TRUE,
            NULL
        )
        """
    )


def downgrade() -> None:
    op.drop_index("ix_dataset_templates_is_default", table_name="dataset_templates")
    op.drop_index("ix_dataset_templates_user_id", table_name="dataset_templates")
    op.drop_table("dataset_templates")
