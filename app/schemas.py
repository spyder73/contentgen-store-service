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
