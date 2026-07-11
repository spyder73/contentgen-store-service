from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class PipelineTemplateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    data: dict[str, Any]
    version: int
    user_id: str | None = None
    visibility: str = "private"
    assigned_user_ids: list[str] = []
    created_at: datetime
    updated_at: datetime


class PipelineTemplateIn(BaseModel):
    id: str
    name: str
    data: dict[str, Any]
    version: int = 1
    user_id: str | None = None
    visibility: str = "private"
    assigned_user_ids: list[str] = []


class PipelineRunSnapshotOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    user_id: str | None = None
    status: str = ""
    snapshot: dict[str, Any] = {}
    created_at: datetime
    updated_at: datetime


class PipelineRunSnapshotIn(BaseModel):
    id: str
    status: str = ""
    snapshot: dict[str, Any] = {}


class PromptTemplateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    content: str
    metadata: dict[str, Any] = {}
    user_id: str | None = None
    visibility: str = "private"
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_orm_row(cls, row) -> "PromptTemplateOut":
        return cls(
            id=row.id,
            name=row.name,
            content=row.content,
            metadata=row.metadata_ or {},
            user_id=row.user_id,
            visibility=getattr(row, "visibility", "private") or "private",
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


class PromptTemplateIn(BaseModel):
    id: str
    name: str
    content: str
    metadata: dict[str, Any] = {}
    user_id: str | None = None
    visibility: str = "private"


class RenderTemplateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    user_id: str | None = None
    name: str
    description: str = ""
    kind: str = "carousel"
    source: str = "user_saved"
    status: str = "active"
    config: dict[str, Any] = Field(default_factory=dict)
    preview_url: str | None = None
    created_from_clip_id: str | None = None
    created_from_instruction: str | None = None
    created_at: datetime
    updated_at: datetime


class RenderTemplateIn(BaseModel):
    id: str | None = None
    name: str
    description: str = ""
    kind: str = "carousel"
    source: str = "user_saved"
    status: str = "active"
    config: dict[str, Any] = Field(default_factory=dict)
    preview_url: str | None = None
    created_from_clip_id: str | None = None
    created_from_instruction: str | None = None


class RenderTemplateCloneIn(BaseModel):
    id: str | None = None
    name: str | None = None


class RenderProposalOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    clip_id: str | None = None
    user_id: str | None = None
    kind: str = "carousel_design"
    status: str = "draft"
    instruction: str = ""
    source_template_id: str | None = None
    metadata_patch_json: dict[str, Any] = Field(default_factory=dict)
    template_config_json: dict[str, Any] = Field(default_factory=dict)
    preview_output_refs_json: Any = Field(default_factory=list)
    validation_report_json: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    approved_at: datetime | None = None


class RenderProposalIn(BaseModel):
    id: str | None = None
    clip_id: str | None = None
    kind: str = "carousel_design"
    status: str = "draft"
    instruction: str = ""
    source_template_id: str | None = None
    metadata_patch_json: dict[str, Any] = Field(default_factory=dict)
    template_config_json: dict[str, Any] = Field(default_factory=dict)
    preview_output_refs_json: Any = Field(default_factory=list)
    validation_report_json: dict[str, Any] = Field(default_factory=dict)


class RenderProposalStatusPatch(BaseModel):
    status: str
    validation_report_json: dict[str, Any] | None = None


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    username: str
    display_name: str = ""
    is_active: bool = True
    is_admin: bool = False
    created_at: datetime


class AccessFeatureOut(BaseModel):
    feature_key: str
    whitelist_enabled: bool
    user_ids: list[str] = []


class UserAccessOut(BaseModel):
    user_id: str
    is_admin: bool = False
    allowed_features: list[str] = []
    visible_tabs: list[str] = []


class AccessFeaturePatch(BaseModel):
    whitelist_enabled: bool


class UserFeatureUpdate(BaseModel):
    feature_keys: list[str]


class PipelineAssignmentsUpdate(BaseModel):
    user_ids: list[str]


class PipelineVisibilityUpdate(BaseModel):
    visibility: str


class ClipPromptOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    metadata: dict[str, Any] = {}
    style: dict[str, Any] = {}
    media_refs: dict[str, Any] = {"images": [], "ai_videos": [], "audios": []}
    render_output_urls: list[Any] = []
    is_dirty: bool = False
    finished_at: datetime | None = None
    thumbnail_url: str | None = None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_orm_row(cls, row) -> "ClipPromptOut":
        return cls(
            id=row.id,
            name=row.name,
            metadata=row.metadata_ or {},
            style=row.style or {},
            media_refs=row.media_refs or {"images": [], "ai_videos": [], "audios": []},
            render_output_urls=row.render_output_urls or [],
            is_dirty=row.is_dirty or False,
            finished_at=row.finished_at,
            thumbnail_url=row.thumbnail_url,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


class ClipPromptIn(BaseModel):
    id: str
    name: str = ""
    metadata: dict[str, Any] = {}
    style: dict[str, Any] = {}
    media_refs: dict[str, Any] = {"images": [], "ai_videos": [], "audios": []}
    render_output_urls: list[Any] = []
    is_dirty: bool = False
    finished_at: datetime | None = None
    thumbnail_url: str | None = None


class MediaItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    clip_id: str | None = None
    type: str
    prompt: str
    file_url: str
    metadata: dict[str, Any] = {}
    output_spec: dict[str, Any] | None = None
    is_favourite: bool = False
    name: str = ""
    pipeline_run_id: str | None = None
    scene_id: str | None = None
    parent_media_id: str | None = None
    role: str | None = None
    # Whether a grid thumbnail can be served for this item (a derivative already
    # exists, or it is an image whose bytes can be lazily thumbnailed). Lets the
    # Go list handler advertise a thumbnail URL only when one will resolve.
    has_thumbnail: bool = False
    # Tiny base64 ``data:`` URI blur-up placeholder, inlined for instant paint.
    # None for non-images / legacy rows (caller falls back to a neutral cell).
    micro_thumb: str | None = None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_orm_row(cls, row) -> "MediaItemOut":
        # thumbnail_content_type / file_mime_type are not deferred, so reading
        # them here triggers no BLOB load. `getattr` guards rows constructed
        # before the 0017 columns existed (e.g. test fixtures).
        has_thumbnail = bool(getattr(row, "thumbnail_content_type", None))
        if not has_thumbnail and row.type == "image":
            has_thumbnail = bool(getattr(row, "file_mime_type", None))
        return cls(
            id=row.id,
            clip_id=row.clip_id,
            type=row.type,
            prompt=row.prompt,
            file_url=row.file_url,
            metadata=row.metadata_ or {},
            output_spec=row.output_spec,
            is_favourite=row.is_favourite or False,
            name=row.name or "",
            pipeline_run_id=row.pipeline_run_id,
            scene_id=row.scene_id,
            parent_media_id=row.parent_media_id,
            role=row.role,
            has_thumbnail=has_thumbnail,
            # micro_thumbnail is a small TEXT column (not deferred) — reading it
            # adds no BLOB I/O. getattr guards pre-0018 rows / test fixtures.
            micro_thumb=getattr(row, "micro_thumbnail", None),
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


class RelatedMediaOut(BaseModel):
    """Lineage of a media item — the parent it derived from, its co-variations
    (siblings sharing a parent), and the variations derived from it. Each is a
    full ``MediaItemOut`` so the inspector can render a navigable thumbnail.
    ``parent`` is None for roots / uploads; the lists are empty when absent.
    """

    parent: MediaItemOut | None = None
    siblings: list[MediaItemOut] = []
    variations: list[MediaItemOut] = []


class MediaItemIn(BaseModel):
    id: str
    clip_id: str | None = None
    type: str
    prompt: str = ""
    file_url: str = ""
    metadata: dict[str, Any] = {}
    output_spec: dict[str, Any] | None = None
    name: str = ""
    pipeline_run_id: str | None = None
    scene_id: str | None = None
    parent_media_id: str | None = None
    role: str | None = None


class ToggleFavouriteBody(BaseModel):
    is_favourite: bool


class SwapClipMediaBody(BaseModel):
    kind: str  # "image" | "ai_video" | "audio"
    media_index: int
    new_media_id: str


class RenameMediaBody(BaseModel):
    name: str


class ClipSummaryOut(BaseModel):
    id: str
    name: str
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None = None
    thumbnail_url: str | None = None
    is_dirty: bool = False
    media_count: dict[str, int] = {"images": 0, "ai_videos": 0, "audios": 0}


class ClipFullOut(BaseModel):
    clip: ClipPromptOut
    media: list[MediaItemOut]


class MediaStatsOut(BaseModel):
    total: int
    image: int
    video: int
    audio: int
    # Source-bucket facet counts (library-wide), so the UI can show correct
    # uploaded/generated totals on the source chips rather than per-page counts.
    # Default 0 keeps older callers / fixtures that don't set them valid.
    uploaded: int = 0
    generated: int = 0


class PagedResponse(BaseModel):
    items: list[Any]
    total: int
    page: int
    limit: int


# ── Series ───────────────────────────────────────────────────────────────

class SeriesOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    description: str = ""
    concept: str = ""
    metadata: dict[str, Any] = {}
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_orm_row(cls, row) -> "SeriesOut":
        return cls(
            id=row.id,
            name=row.name,
            description=row.description,
            concept=row.concept,
            metadata=row.metadata_ or {},
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


class SeriesIn(BaseModel):
    id: str
    name: str
    description: str = ""
    concept: str = ""
    metadata: dict[str, Any] = {}


# ── Character ────────────────────────────────────────────────────────────

class CharacterOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    series_id: str
    name: str
    description: str = ""
    voice: str = ""
    reference_image_media_id: str | None = None
    generator_profile_id: str | None = None
    metadata: dict[str, Any] = {}
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_orm_row(cls, row) -> "CharacterOut":
        return cls(
            id=row.id,
            series_id=row.series_id,
            name=row.name,
            description=row.description,
            voice=row.voice,
            reference_image_media_id=row.reference_image_media_id,
            generator_profile_id=row.generator_profile_id,
            metadata=row.metadata_ or {},
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


class CharacterIn(BaseModel):
    id: str
    series_id: str
    name: str
    description: str = ""
    voice: str = ""
    reference_image_media_id: str | None = None
    generator_profile_id: str | None = None
    metadata: dict[str, Any] = {}


# ── Episode ──────────────────────────────────────────────────────────────

class EpisodeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    series_id: str
    episode_number: int
    title: str = ""
    synopsis: str = ""
    prev_episode_summary: str = ""
    metadata: dict[str, Any] = {}
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_orm_row(cls, row) -> "EpisodeOut":
        return cls(
            id=row.id,
            series_id=row.series_id,
            episode_number=row.episode_number,
            title=row.title,
            synopsis=row.synopsis,
            prev_episode_summary=row.prev_episode_summary,
            metadata=row.metadata_ or {},
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


class EpisodeIn(BaseModel):
    id: str
    series_id: str
    episode_number: int
    title: str = ""
    synopsis: str = ""
    prev_episode_summary: str = ""
    metadata: dict[str, Any] = {}


# ── VoiceSnippet ─────────────────────────────────────────────────────────

class VoiceSnippetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    character_id: str
    file_url: str = ""
    duration: float = 0.0
    metadata: dict[str, Any] = {}
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_orm_row(cls, row) -> "VoiceSnippetOut":
        return cls(
            id=row.id,
            character_id=row.character_id,
            file_url=row.file_url,
            duration=row.duration,
            metadata=row.metadata_ or {},
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


# ── GeneratorProfile ─────────────────────────────────────────────────────

class GeneratorProfileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    slug: str
    version: int
    status: str
    user_id: str | None = None
    name: str
    media_type: str = "image"
    spec: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class GeneratorProfileCreate(BaseModel):
    slug: str
    name: str
    media_type: str = "image"
    user_id: str | None = None
    spec: dict[str, Any] = Field(default_factory=dict)


class GeneratorProfileUpdate(BaseModel):
    name: str | None = None
    spec: dict[str, Any] | None = None


# ── DatasetTemplate ───────────────────────────────────────────────────────

# NOTE: this module uses `from __future__ import annotations`, so any new
# pydantic model referenced by the DatasetTemplate schemas MUST be defined at
# module scope (not nested/locally) or forward-ref resolution 422s at runtime.
class CollageStage(BaseModel):
    label: str
    purpose: str | None = None
    prompt: str
    width: int
    height: int
    grid_x: int
    grid_y: int
    inset_pct: float = 0.015
    reference_policy: str
    model: str | None = None
    # Explicit list of 1-based stage indexes whose collages should be fed as
    # references for this stage. When set (non-empty) it takes precedence over
    # `reference_policy`; None/empty falls back to the policy string. Lets a
    # stage wire specific prior collages (e.g. a face-rotation grid + a
    # full-body reference collage) instead of the positional policy heuristics.
    reference_stage_indexes: list[int] | None = None


class DatasetTemplateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    user_id: str | None = None
    name: str
    description: str | None = None
    collage_prompt: str
    collage_model: str = "openai:gpt-image@2"
    collage_width: int = 3840
    collage_height: int = 2160
    collage_quality: str | None = "high"
    split_grid_x: int = 4
    split_grid_y: int = 4
    upscale_enabled: bool = True
    upscale_model: str | None = "prunaai:p-image@upscale"
    target_megapixels: int | None = 4
    upscale_enhance_details: bool | None = False
    upscale_realism: bool | None = False
    caption_vision_model: str | None = "google/gemini-2.5-flash"
    caption_format: str = "{{trigger_token}}, {{description}}"
    model_target: str = "sdxl"
    collage_stages: list[CollageStage] | None = None
    seed_reference_media_id: str | None = None
    is_default: bool = False
    created_at: datetime
    updated_at: datetime


class DatasetTemplateCreate(BaseModel):
    name: str
    collage_prompt: str
    user_id: str | None = None
    description: str | None = None
    collage_model: str = "openai:gpt-image@2"
    collage_width: int = 3840
    collage_height: int = 2160
    collage_quality: str = "high"
    split_grid_x: int = 4
    split_grid_y: int = 4
    upscale_enabled: bool = True
    upscale_model: str = "prunaai:p-image@upscale"
    target_megapixels: int = 4
    upscale_enhance_details: bool = False
    upscale_realism: bool = False
    caption_vision_model: str = "google/gemini-2.5-flash"
    caption_format: str = "{{trigger_token}}, {{description}}"
    model_target: str = "sdxl"
    collage_stages: list[CollageStage] | None = None
    seed_reference_media_id: str | None = None
    is_default: bool = False


class DatasetTemplateUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    collage_prompt: str | None = None
    collage_model: str | None = None
    collage_width: int | None = None
    collage_height: int | None = None
    collage_quality: str | None = None
    split_grid_x: int | None = None
    split_grid_y: int | None = None
    upscale_enabled: bool | None = None
    upscale_model: str | None = None
    target_megapixels: int | None = None
    upscale_enhance_details: bool | None = None
    upscale_realism: bool | None = None
    caption_vision_model: str | None = None
    caption_format: str | None = None
    model_target: str | None = None
    collage_stages: list[CollageStage] | None = None
    seed_reference_media_id: str | None = None
    is_default: bool | None = None
