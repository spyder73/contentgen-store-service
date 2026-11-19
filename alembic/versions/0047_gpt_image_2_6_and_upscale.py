"""saved GPT Image 2.5 Flare picks move to 2.6 Flare; upscale joins saved video-edit lists

Revision ID: 0047
Revises: 0046
Create Date: 2026-11-19

GPT Image 2.6 Flare replaces 2.5 Flare at the same price. A user's saved settings may hold the
old id in ``generation.image.model`` and in each ``agent_tools.image_models`` entry; both move
to the new id, and entries that then repeat (same provider and model) collapse to the first.

P-Video-Upscale becomes a video-edit model. A saved ``agent_tools.video_edit_models`` list of 1
to 4 entries that lacks it gains it as the last entry (the cap is 5). An absent key already reads
as the defaults, which hold upscale, and a saved ``[]`` is the user's "none" and stays empty.

Deploy after the backend that allows five saved edit models.
"""
from __future__ import annotations

import copy
import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0047"
down_revision: Union[str, None] = "0046"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OLD = "openai:gpt-image@2.5-flare"
NEW = "openai:gpt-image@2.6-flare"
UPSCALE = {"provider": "runware", "model": "prunaai:p-video@upscale"}
MAX_EDIT_MODELS = 5


def moved_settings(settings: dict) -> dict | None:
    """A deep copy of ``settings`` with the moves applied, or None when nothing changed."""
    out = copy.deepcopy(settings)
    changed = False

    gen = out.get("generation")
    image = gen.get("image") if isinstance(gen, dict) else None
    if isinstance(image, dict) and image.get("model") == OLD:
        image["model"] = NEW
        changed = True

    tools = out.get("agent_tools")
    if isinstance(tools, dict):
        models = tools.get("image_models")
        if isinstance(models, list) and any(isinstance(e, dict) and e.get("model") == OLD for e in models):
            seen, kept = set(), []
            for e in models:
                if isinstance(e, dict) and e.get("model") == OLD:
                    e["model"] = NEW
                key = (e.get("provider"), e.get("model")) if isinstance(e, dict) else None
                if key is not None:
                    if key in seen:
                        continue
                    seen.add(key)
                kept.append(e)
            tools["image_models"] = kept
            changed = True

        edit = tools.get("video_edit_models")
        if (
            isinstance(edit, list)
            and 1 <= len(edit) < MAX_EDIT_MODELS
            and UPSCALE not in edit
        ):
            edit.append(copy.deepcopy(UPSCALE))
            changed = True

    return out if changed else None


def upgrade() -> None:
    bind = op.get_bind()
    table = sa.table(
        "user_settings",
        sa.column("user_id"),
        sa.column("settings", postgresql.JSONB().with_variant(sa.JSON(), "sqlite")),
    )
    rows = bind.execute(
        sa.text(
            "SELECT user_id, settings FROM user_settings "
            "WHERE CAST(settings AS TEXT) LIKE '%gpt-image@2.5-flare%' "
            "OR CAST(settings AS TEXT) LIKE '%video_edit_models%'"
        )
    ).fetchall()
    for user_id, settings in rows:
        if isinstance(settings, str):
            settings = json.loads(settings)
        if not isinstance(settings, dict):
            continue
        moved = moved_settings(settings)
        if moved is not None:
            bind.execute(table.update().where(table.c.user_id == user_id).values(settings=moved))


def downgrade() -> None:
    # 2.5 and 2.6 Flare cost the same and upscale is an ordinary pick; a user can change either in Settings.
    pass
