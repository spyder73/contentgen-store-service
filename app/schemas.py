from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class PipelineTemplateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    data: dict[str, Any]
    version: int
    created_at: datetime
    updated_at: datetime


class PipelineTemplateIn(BaseModel):
    id: str
    name: str
    data: dict[str, Any]
    version: int = 1


class PromptTemplateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    content: str
    metadata: dict[str, Any] = {}
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_orm_row(cls, row) -> "PromptTemplateOut":
        return cls(
            id=row.id,
            name=row.name,
            content=row.content,
            metadata=row.metadata_ or {},
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


class PromptTemplateIn(BaseModel):
    id: str
    name: str
    content: str
    metadata: dict[str, Any] = {}


class ClipPromptOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    metadata: dict[str, Any] = {}
    style: dict[str, Any] = {}
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_orm_row(cls, row) -> "ClipPromptOut":
        return cls(
            id=row.id,
            name=row.name,
            metadata=row.metadata_ or {},
            style=row.style or {},
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


class ClipPromptIn(BaseModel):
    id: str
    name: str = ""
    metadata: dict[str, Any] = {}
    style: dict[str, Any] = {}


class MediaItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    clip_id: str | None = None
    type: str
    prompt: str
    file_url: str
    metadata: dict[str, Any] = {}
    output_spec: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_orm_row(cls, row) -> "MediaItemOut":
        return cls(
            id=row.id,
            clip_id=row.clip_id,
            type=row.type,
            prompt=row.prompt,
            file_url=row.file_url,
            metadata=row.metadata_ or {},
            output_spec=row.output_spec,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


class MediaItemIn(BaseModel):
    id: str
    clip_id: str | None = None
    type: str
    prompt: str = ""
    file_url: str = ""
    metadata: dict[str, Any] = {}
    output_spec: dict[str, Any] | None = None


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
