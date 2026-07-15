from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, Float, ForeignKey, Index, Integer, LargeBinary, Numeric, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    username: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="TRUE")
    credits_balance: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    credits_reserved: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    is_admin: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="FALSE"
    )
    daily_spend_limit: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=5000, server_default="5000"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class CreditsLedger(Base):
    __tablename__ = "credits_ledger"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, server_default=func.gen_random_uuid()
    )
    user_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    delta: Mapped[int] = mapped_column(BigInteger, nullable=False)
    pipeline_run_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False), nullable=True
    )
    checkpoint_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    provider: Mapped[str | None] = mapped_column(Text, nullable=True)
    model: Mapped[str | None] = mapped_column(Text, nullable=True)
    cost_usd: Mapped[float | None] = mapped_column(Numeric(12, 6), nullable=True)
    cost_source: Mapped[str | None] = mapped_column(Text, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    admin_user_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    idempotency_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "kind IN ('grant','hold','release','debit','adjust')",
            name="credits_ledger_kind_valid",
        ),
    )


class PipelineTemplate(Base):
    __tablename__ = "pipeline_templates"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    data: Mapped[dict] = mapped_column(JSONB, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1)
    user_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    visibility: Mapped[str] = mapped_column(Text, nullable=False, default="private", server_default="private")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class PipelineTemplateAssignment(Base):
    __tablename__ = "pipeline_template_assignments"

    template_id: Mapped[str] = mapped_column(
        Text, ForeignKey("pipeline_templates.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        Index("ix_pipeline_template_assignments_user_id", "user_id"),
    )


class PromptTemplate(Base):
    __tablename__ = "prompt_templates"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)
    user_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    visibility: Mapped[str] = mapped_column(Text, nullable=False, default="private", server_default="private")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class RenderTemplate(Base):
    __tablename__ = "render_templates"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    kind: Mapped[str] = mapped_column(Text, nullable=False, default="carousel", server_default="carousel")
    source: Mapped[str] = mapped_column(Text, nullable=False, default="user_saved", server_default="user_saved")
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active", server_default="active")
    config: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    preview_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_from_clip_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_from_instruction: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "source IN ('builtin','user_saved','agent_generated')",
            name="render_templates_source_valid",
        ),
        CheckConstraint(
            "status IN ('draft','active','archived')",
            name="render_templates_status_valid",
        ),
        Index("ix_render_templates_user_id", "user_id"),
        Index("ix_render_templates_kind_status", "kind", "status"),
    )


class RenderProposal(Base):
    __tablename__ = "render_proposals"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    clip_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False, default="carousel_design", server_default="carousel_design")
    status: Mapped[str] = mapped_column(Text, nullable=False, default="draft", server_default="draft")
    instruction: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    source_template_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("render_templates.id", ondelete="SET NULL"), nullable=True
    )
    metadata_patch_json: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    template_config_json: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    preview_output_refs_json: Mapped[dict | list] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    validation_report_json: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status IN ('draft','validated','approved','rejected','failed')",
            name="render_proposals_status_valid",
        ),
        Index("ix_render_proposals_user_id", "user_id"),
        Index("ix_render_proposals_clip_id", "clip_id"),
        Index("ix_render_proposals_status", "status"),
    )


class AccessFeature(Base):
    __tablename__ = "access_features"

    feature_key: Mapped[str] = mapped_column(Text, primary_key=True)
    whitelist_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="TRUE"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AccessFeatureUser(Base):
    __tablename__ = "access_feature_users"

    feature_key: Mapped[str] = mapped_column(
        Text, ForeignKey("access_features.feature_key", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        Index("ix_access_feature_users_user_id", "user_id"),
        UniqueConstraint("feature_key", "user_id", name="uq_access_feature_users_feature_user"),
    )


class SystemPrompt(Base):
    __tablename__ = "system_prompts"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ClipPrompt(Base):
    __tablename__ = "clip_prompts"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    user_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    name: Mapped[str] = mapped_column(Text, default="")
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)
    style: Mapped[dict] = mapped_column(JSONB, default=dict)
    media_refs: Mapped[dict] = mapped_column(
        JSONB, default=lambda: {"images": [], "ai_videos": [], "audios": []}
    )
    render_output_urls: Mapped[list] = mapped_column(JSONB, default=list)
    is_dirty: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="FALSE"
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    thumbnail_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class MediaItem(Base):
    __tablename__ = "media_items"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    user_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    clip_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("clip_prompts.id", ondelete="SET NULL"),
        nullable=True,
    )
    type: Mapped[str] = mapped_column(Text, nullable=False)
    prompt: Mapped[str] = mapped_column(Text, default="")
    file_url: Mapped[str] = mapped_column(Text, default="")
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)
    output_spec: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    is_favourite: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="FALSE")
    name: Mapped[str] = mapped_column(Text, default="")
    pipeline_run_id: Mapped[str | None] = mapped_column(UUID(as_uuid=False), nullable=True)
    scene_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    parent_media_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("media_items.id", ondelete="SET NULL"),
        nullable=True,
    )
    role: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_data: Mapped[bytes | None] = mapped_column(LargeBinary(), nullable=True)
    file_mime_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Small derived thumbnail (webp, ≤512px long edge) used by the library grid so
    # cells never load the full-resolution original. Generated at file-store time
    # for images and lazily backfilled on first thumbnail GET for legacy rows.
    # Both nullable/additive (migration 0017): a NULL thumbnail falls back to the
    # original, preserving pre-thumbnail behaviour.
    thumbnail_data: Mapped[bytes | None] = mapped_column(LargeBinary(), nullable=True)
    thumbnail_content_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Tiny (~28px) webp blur-up placeholder stored as a base64 ``data:`` URI
    # *string* (a few hundred bytes). Unlike thumbnail_data this is small TEXT —
    # it is inlined directly in the list JSON so cells never flash empty while
    # the real thumbnail loads, with zero extra HTTP requests. Nullable/additive
    # (migration 0018): NULL falls back to the existing neutral cell.
    micro_thumbnail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index("ix_media_items_clip_id", "clip_id"),
        Index("ix_media_items_type", "type"),
        Index("ix_media_items_created_at", "created_at"),
        Index("ix_media_items_pipeline_run_id", "pipeline_run_id"),
        Index("ix_media_items_scene_id", "scene_id"),
        Index("ix_media_items_name", "name"),
        Index("ix_media_items_pipeline_scene_type", "pipeline_run_id", "scene_id", "type"),
        # The library list access pattern: WHERE user_id=? ORDER BY created_at DESC.
        # Matching the index order to the sort lets Postgres serve the page
        # without a separate sort step. Created in migration 0016.
        Index(
            "ix_media_items_user_created_desc",
            "user_id",
            created_at.desc(),
        ),
        # Favourites filter: WHERE user_id=? AND is_favourite=?. Created in 0016.
        Index("ix_media_items_user_favourite", "user_id", "is_favourite"),
    )


class Series(Base):
    __tablename__ = "series"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    user_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    concept: Mapped[str] = mapped_column(Text, default="")
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Character(Base):
    __tablename__ = "characters"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    series_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("series.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    voice: Mapped[str] = mapped_column(Text, default="")
    reference_image_media_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("media_items.id", ondelete="SET NULL"),
        nullable=True,
    )
    generator_profile_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("generator_profiles.id", ondelete="SET NULL"),
        nullable=True,
    )
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index("ix_characters_series_id", "series_id"),
    )


class GeneratorProfile(Base):
    """A versioned image/video generator definition (base model + adapters + params).

    A ``slug`` groups versions; ``(slug, version)`` is unique. A slug may hold at
    most one ``draft`` at a time, plus any number of ``published`` (frozen)
    versions. Publishing freezes a row; drafts are the only mutable/deletable
    rows. Characters reference a profile via ``generator_profile_id``.
    """

    __tablename__ = "generator_profiles"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, server_default=func.gen_random_uuid()
    )
    slug: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="draft", server_default="draft")
    user_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    media_type: Mapped[str] = mapped_column(Text, nullable=False, default="image", server_default="image")
    spec: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("slug", "version", name="uq_generator_profiles_slug_version"),
        CheckConstraint(
            "status IN ('draft','published')",
            name="generator_profiles_status_valid",
        ),
        Index("ix_generator_profiles_slug_version", "slug", "version", unique=True),
        Index("ix_generator_profiles_status", "status"),
    )


class PuppetPosePreset(Base):
    """A reusable Puppet pose owned by one user."""

    __tablename__ = "puppet_pose_presets"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, server_default=func.gen_random_uuid()
    )
    user_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_hint: Mapped[str] = mapped_column(Text, nullable=False)
    config: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_puppet_pose_presets_user_name"),
        Index("ix_puppet_pose_presets_user_id", "user_id"),
    )


class Episode(Base):
    __tablename__ = "episodes"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    series_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("series.id", ondelete="CASCADE"),
        nullable=False,
    )
    episode_number: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(Text, default="")
    synopsis: Mapped[str] = mapped_column(Text, default="")
    prev_episode_summary: Mapped[str] = mapped_column(Text, default="")
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index("ix_episodes_series_id", "series_id"),
    )


class VoiceSnippet(Base):
    __tablename__ = "voice_snippets"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    character_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("characters.id", ondelete="CASCADE"),
        nullable=False,
    )
    file_url: Mapped[str] = mapped_column(Text, default="")
    duration: Mapped[float] = mapped_column(Float, default=0.0)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index("ix_voice_snippets_character_id", "character_id"),
    )


class PipelineRunSnapshot(Base):
    """Persisted snapshot of a pipeline run so runs survive a backend restart.

    The full run state is serialised into the JSONB ``snapshot`` column by the
    Go backend; ``status`` is duplicated as a top-level column purely so the
    backend's rehydration policy can be applied without unmarshalling every
    snapshot.
    """

    __tablename__ = "pipeline_run_snapshots"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    user_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    snapshot: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index("ix_pipeline_run_snapshots_user_id", "user_id"),
        Index("ix_pipeline_run_snapshots_status", "status"),
    )


class DatasetTemplate(Base):
    """User-editable template for collage-based dataset generation.

    Stores all settings for collage generation, splitting, upscaling, and captioning.
    System templates have user_id=NULL; user templates are owned by a specific user.
    At most one template can have is_default=TRUE.
    """

    __tablename__ = "dataset_templates"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, server_default=func.gen_random_uuid()
    )
    user_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    collage_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    collage_model: Mapped[str] = mapped_column(
        Text, nullable=False, default="openai:gpt-image@2", server_default="openai:gpt-image@2"
    )
    collage_width: Mapped[int] = mapped_column(Integer, nullable=False, default=3840)
    collage_height: Mapped[int] = mapped_column(Integer, nullable=False, default=2160)
    collage_quality: Mapped[str | None] = mapped_column(Text, default="high")
    split_grid_x: Mapped[int] = mapped_column(Integer, nullable=False, default=4)
    split_grid_y: Mapped[int] = mapped_column(Integer, nullable=False, default=4)
    upscale_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="TRUE"
    )
    upscale_model: Mapped[str | None] = mapped_column(Text, default="prunaai:p-image@upscale")
    target_megapixels: Mapped[int | None] = mapped_column(Integer, default=4)
    upscale_enhance_details: Mapped[bool | None] = mapped_column(Boolean, default=False)
    upscale_realism: Mapped[bool | None] = mapped_column(Boolean, default=False)
    caption_vision_model: Mapped[str | None] = mapped_column(
        Text, default="google/gemini-2.5-flash"
    )
    caption_format: Mapped[str] = mapped_column(
        Text, nullable=False, default="{{trigger_token}}, {{description}}",
        server_default="{{trigger_token}}, {{description}}"
    )
    model_target: Mapped[str] = mapped_column(
        Text, nullable=False, default="sdxl", server_default="sdxl"
    )
    # Multi-stage collage recipe (NULL => legacy single-prompt mode). Each element
    # is a CollageStage dict (label/prompt/width/height/grid_x/grid_y/inset_pct/
    # reference_policy). See migration 0022.
    collage_stages: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    # Seed/avatar reference media id chained forward for identity consistency.
    seed_reference_media_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_default: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="FALSE"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index("ix_dataset_templates_user_id", "user_id"),
        Index(
            "ix_dataset_templates_is_default",
            "is_default",
            postgresql_where=text("is_default = true"),
        ),
    )
