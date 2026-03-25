from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_session
from .schemas import (
    CharacterIn,
    CharacterOut,
    ClipPromptIn,
    ClipPromptOut,
    EpisodeIn,
    EpisodeOut,
    MediaItemIn,
    MediaItemOut,
    PagedResponse,
    PipelineTemplateIn,
    PipelineTemplateOut,
    PromptTemplateIn,
    PromptTemplateOut,
    SeriesIn,
    SeriesOut,
    VoiceSnippetOut,
)
from .stores import characters, clips, episodes, media, pipelines, prompts, series, voice_snippets

logger = logging.getLogger(__name__)

SessionDep = Annotated[AsyncSession, Depends(get_session)]


def create_fastapi_app() -> FastAPI:
    app = FastAPI(title="contentgen-store-service")

    # ── health ──────────────────────────────────────────────────────────────

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    # ── pipeline templates ──────────────────────────────────────────────────

    @app.get("/v1/pipelines", response_model=list[PipelineTemplateOut])
    async def list_pipelines_handler(session: SessionDep) -> Any:
        return await pipelines.list_pipelines(session)

    @app.get("/v1/pipelines/{id}", response_model=PipelineTemplateOut)
    async def get_pipeline_handler(id: str, session: SessionDep) -> Any:
        row = await pipelines.get_pipeline(session, id)
        if row is None:
            raise HTTPException(status_code=404, detail="not_found")
        return row

    @app.put("/v1/pipelines/{id}", response_model=PipelineTemplateOut)
    async def upsert_pipeline_handler(id: str, body: PipelineTemplateIn, session: SessionDep) -> Any:
        body.id = id
        return await pipelines.upsert_pipeline(session, body)

    @app.delete("/v1/pipelines/{id}", status_code=204)
    async def delete_pipeline_handler(id: str, session: SessionDep) -> None:
        deleted = await pipelines.delete_pipeline(session, id)
        if not deleted:
            raise HTTPException(status_code=404, detail="not_found")

    # ── prompt templates ────────────────────────────────────────────────────

    @app.get("/v1/prompts", response_model=list[PromptTemplateOut])
    async def list_prompts_handler(session: SessionDep) -> Any:
        return await prompts.list_prompts(session)

    @app.get("/v1/prompts/{id}", response_model=PromptTemplateOut)
    async def get_prompt_handler(id: str, session: SessionDep) -> Any:
        row = await prompts.get_prompt(session, id)
        if row is None:
            raise HTTPException(status_code=404, detail="not_found")
        return row

    @app.put("/v1/prompts/{id}", response_model=PromptTemplateOut)
    async def upsert_prompt_handler(id: str, body: PromptTemplateIn, session: SessionDep) -> Any:
        body.id = id
        return await prompts.upsert_prompt(session, body)

    @app.delete("/v1/prompts/{id}", status_code=204)
    async def delete_prompt_handler(id: str, session: SessionDep) -> None:
        deleted = await prompts.delete_prompt(session, id)
        if not deleted:
            raise HTTPException(status_code=404, detail="not_found")

    # ── clips ───────────────────────────────────────────────────────────────

    @app.get("/v1/clips", response_model=PagedResponse)
    async def list_clips_handler(
        session: SessionDep,
        page: int = Query(1, ge=1),
        limit: int = Query(50, ge=1, le=200),
    ) -> Any:
        return await clips.list_clips(session, page=page, limit=limit)

    @app.get("/v1/clips/{id}", response_model=ClipPromptOut)
    async def get_clip_handler(id: str, session: SessionDep) -> Any:
        row = await clips.get_clip(session, id)
        if row is None:
            raise HTTPException(status_code=404, detail="not_found")
        return row

    @app.put("/v1/clips/{id}", response_model=ClipPromptOut)
    async def upsert_clip_handler(id: str, body: ClipPromptIn, session: SessionDep) -> Any:
        body.id = id
        return await clips.upsert_clip(session, body)

    @app.delete("/v1/clips/{id}", status_code=204)
    async def delete_clip_handler(id: str, session: SessionDep) -> None:
        deleted = await clips.delete_clip(session, id)
        if not deleted:
            raise HTTPException(status_code=404, detail="not_found")

    # ── media items ─────────────────────────────────────────────────────────

    @app.get("/v1/media", response_model=PagedResponse)
    async def list_media_handler(
        session: SessionDep,
        clip_id: str | None = Query(None),
        type: str | None = Query(None),
        search: str | None = Query(None),
        page: int = Query(1, ge=1),
        limit: int = Query(50, ge=1, le=200),
    ) -> Any:
        return await media.list_media(session, clip_id=clip_id, type_=type, search=search, page=page, limit=limit)

    @app.get("/v1/media/{id}", response_model=MediaItemOut)
    async def get_media_handler(id: str, session: SessionDep) -> Any:
        row = await media.get_media(session, id)
        if row is None:
            raise HTTPException(status_code=404, detail="not_found")
        return row

    @app.put("/v1/media/{id}", response_model=MediaItemOut)
    async def upsert_media_handler(id: str, body: MediaItemIn, session: SessionDep) -> Any:
        body.id = id
        return await media.upsert_media(session, body)

    @app.delete("/v1/media/{id}", status_code=204)
    async def delete_media_handler(id: str, session: SessionDep) -> None:
        deleted = await media.delete_media(session, id)
        if not deleted:
            raise HTTPException(status_code=404, detail="not_found")

    # ── series ───────────────────────────────────────────────────────────────

    @app.get("/v1/series", response_model=PagedResponse)
    async def list_series_handler(
        session: SessionDep,
        page: int = Query(1, ge=1),
        limit: int = Query(50, ge=1, le=200),
    ) -> Any:
        return await series.list_series(session, page=page, limit=limit)

    @app.get("/v1/series/{id}", response_model=SeriesOut)
    async def get_series_handler(id: str, session: SessionDep) -> Any:
        row = await series.get_series(session, id)
        if row is None:
            raise HTTPException(status_code=404, detail="not_found")
        return row

    @app.put("/v1/series/{id}", response_model=SeriesOut)
    async def upsert_series_handler(id: str, body: SeriesIn, session: SessionDep) -> Any:
        body.id = id
        return await series.upsert_series(session, body)

    @app.delete("/v1/series/{id}", status_code=204)
    async def delete_series_handler(id: str, session: SessionDep) -> None:
        deleted = await series.delete_series(session, id)
        if not deleted:
            raise HTTPException(status_code=404, detail="not_found")

    # ── characters ───────────────────────────────────────────────────────────

    @app.get("/v1/characters", response_model=PagedResponse)
    async def list_characters_handler(
        session: SessionDep,
        series_id: str | None = Query(None),
        page: int = Query(1, ge=1),
        limit: int = Query(50, ge=1, le=200),
    ) -> Any:
        return await characters.list_characters(session, series_id=series_id, page=page, limit=limit)

    @app.get("/v1/characters/{id}", response_model=CharacterOut)
    async def get_character_handler(id: str, session: SessionDep) -> Any:
        row = await characters.get_character(session, id)
        if row is None:
            raise HTTPException(status_code=404, detail="not_found")
        return row

    @app.put("/v1/characters/{id}", response_model=CharacterOut)
    async def upsert_character_handler(id: str, body: CharacterIn, session: SessionDep) -> Any:
        body.id = id
        return await characters.upsert_character(session, body)

    @app.delete("/v1/characters/{id}", status_code=204)
    async def delete_character_handler(id: str, session: SessionDep) -> None:
        deleted = await characters.delete_character(session, id)
        if not deleted:
            raise HTTPException(status_code=404, detail="not_found")

    # ── episodes ─────────────────────────────────────────────────────────────

    @app.get("/v1/episodes", response_model=PagedResponse)
    async def list_episodes_handler(
        session: SessionDep,
        series_id: str | None = Query(None),
        page: int = Query(1, ge=1),
        limit: int = Query(50, ge=1, le=200),
    ) -> Any:
        return await episodes.list_episodes(session, series_id=series_id, page=page, limit=limit)

    @app.get("/v1/episodes/{id}", response_model=EpisodeOut)
    async def get_episode_handler(id: str, session: SessionDep) -> Any:
        row = await episodes.get_episode(session, id)
        if row is None:
            raise HTTPException(status_code=404, detail="not_found")
        return row

    @app.put("/v1/episodes/{id}", response_model=EpisodeOut)
    async def upsert_episode_handler(id: str, body: EpisodeIn, session: SessionDep) -> Any:
        body.id = id
        return await episodes.upsert_episode(session, body)

    @app.delete("/v1/episodes/{id}", status_code=204)
    async def delete_episode_handler(id: str, session: SessionDep) -> None:
        deleted = await episodes.delete_episode(session, id)
        if not deleted:
            raise HTTPException(status_code=404, detail="not_found")

    # ── voice snippets ───────────────────────────────────────────────────────

    @app.get("/v1/voice-snippets", response_model=PagedResponse)
    async def list_voice_snippets_handler(
        session: SessionDep,
        character_id: str | None = Query(None),
        page: int = Query(1, ge=1),
        limit: int = Query(50, ge=1, le=200),
    ) -> Any:
        return await voice_snippets.list_voice_snippets(session, character_id=character_id, page=page, limit=limit)

    @app.get("/v1/voice-snippets/{id}", response_model=VoiceSnippetOut)
    async def get_voice_snippet_handler(id: str, session: SessionDep) -> Any:
        row = await voice_snippets.get_voice_snippet(session, id)
        if row is None:
            raise HTTPException(status_code=404, detail="not_found")
        return row

    @app.delete("/v1/voice-snippets/{id}", status_code=204)
    async def delete_voice_snippet_handler(id: str, session: SessionDep) -> None:
        deleted = await voice_snippets.delete_voice_snippet(session, id)
        if not deleted:
            raise HTTPException(status_code=404, detail="not_found")

    # ── error handler ────────────────────────────────────────────────────────

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request, exc: Exception):
        logger.exception("unhandled error: %s", exc)
        return JSONResponse(status_code=500, content={"error": "internal_server_error"})

    return app
