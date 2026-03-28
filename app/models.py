from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


class PipelineTemplate(Base):
    __tablename__ = "pipeline_templates"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    data: Mapped[dict] = mapped_column(JSONB, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class PromptTemplate(Base):
    __tablename__ = "prompt_templates"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ClipPrompt(Base):
    __tablename__ = "clip_prompts"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    name: Mapped[str] = mapped_column(Text, default="")
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)
    style: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class MediaItem(Base):
    __tablename__ = "media_items"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
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
    )


class Series(Base):
    __tablename__ = "series"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
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
