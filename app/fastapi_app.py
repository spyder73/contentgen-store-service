from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from pydantic import BaseModel

from .db import get_session
from .schemas import (
    CharacterIn,
    CharacterOut,
    ClipFullOut,
    ClipPromptIn,
    ClipPromptOut,
    ClipSummaryOut,
    EpisodeIn,
    EpisodeOut,
    MediaItemIn,
    MediaItemOut,
    MediaStatsOut,
    PagedResponse,
    PipelineTemplateIn,
    PipelineTemplateOut,
    PromptTemplateIn,
    PromptTemplateOut,
    RenameMediaBody,
    SeriesIn,
    SeriesOut,
    SwapClipMediaBody,
    ToggleFavouriteBody,
    VoiceSnippetOut,
)

from .stores import characters, clips, episodes, media, pipelines, prompts, series, system_prompts, voice_snippets

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

    # ── system prompts ──────────────────────────────────────────────────────

    @app.get("/v1/system-prompts/{id}", response_model=str)
    async def get_system_prompt_handler(id: str, session: SessionDep) -> Any:
        content = await system_prompts.get_system_prompt(session, id)
        if content is None:
            raise HTTPException(status_code=404, detail="not_found")
        return content

    @app.put("/v1/system-prompts/{id}", response_model=str)
    async def upsert_system_prompt_handler(id: str, body: dict, session: SessionDep) -> Any:
        content = body.get("content", "")
        if not content:
            raise HTTPException(status_code=400, detail="content is required")
        return await system_prompts.upsert_system_prompt(session, id, content)

    # ── clips ───────────────────────────────────────────────────────────────

    @app.get("/v1/clips", response_model=PagedResponse)
    async def list_clips_handler(
        session: SessionDep,
        page: int = Query(1, ge=1),
        limit: int = Query(50, ge=1, le=200),
    ) -> Any:
        return await clips.list_clips(session, page=page, limit=limit)

    # NOTE: /v1/clips/summary must be registered before /v1/clips/{id}
    @app.get("/v1/clips/summary", response_model=PagedResponse)
    async def list_clip_summaries_handler(
        session: SessionDep,
        page: int = Query(1, ge=1),
        limit: int = Query(50, ge=1, le=200),
        finished_only: bool = Query(False),
    ) -> Any:
        return await clips.list_clip_summaries(
            session, page=page, limit=limit, finished_only=finished_only
        )

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

    @app.get("/v1/clips/{id}/full", response_model=ClipFullOut)
    async def get_full_clip_handler(id: str, session: SessionDep) -> Any:
        result = await clips.get_full_clip(session, id)
        if result is None:
            raise HTTPException(status_code=404, detail="not_found")
        return result

    @app.post("/v1/clips/{id}/swap", response_model=ClipFullOut)
    async def swap_clip_media_handler(
        id: str, body: SwapClipMediaBody, session: SessionDep
    ) -> Any:
        try:
            result = await clips.swap_clip_media(session, id, body)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except (ValueError, IndexError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if result is None:
            raise HTTPException(status_code=404, detail="not_found")
        return result

    # ── media items ─────────────────────────────────────────────────────────

    # NOTE: /v1/media/stats must be registered before /v1/media/{id}
    @app.get("/v1/media/stats", response_model=MediaStatsOut)
    async def get_media_stats_handler(session: SessionDep) -> Any:
        return await media.get_media_stats(session)

    @app.get("/v1/media", response_model=PagedResponse)
    async def list_media_handler(
        session: SessionDep,
        clip_id: str | None = Query(None),
        type: str | None = Query(None),
        search: str | None = Query(None),
        is_favourite: bool | None = Query(None),
        pipeline_run_id: str | None = Query(None),
        scene_id: str | None = Query(None),
        role: str | None = Query(None),
        page: int = Query(1, ge=1),
        limit: int = Query(50, ge=1, le=200),
    ) -> Any:
        return await media.list_media(
            session,
            clip_id=clip_id,
            type_=type,
            search=search,
            is_favourite=is_favourite,
            pipeline_run_id=pipeline_run_id,
            scene_id=scene_id,
            role=role,
            page=page,
            limit=limit,
        )

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

    @app.patch("/v1/media/{id}/favourite", response_model=MediaItemOut)
    async def toggle_media_favourite(
        id: str,
        body: ToggleFavouriteBody,
        session: SessionDep,
    ) -> Any:
        result = await media.toggle_favourite(session, id, body.is_favourite)
        if result is None:
            raise HTTPException(status_code=404, detail="Media item not found")
        return result

    @app.put("/v1/media/{id}/rename", response_model=MediaItemOut)
    async def rename_media_handler(
        id: str,
        body: RenameMediaBody,
        session: SessionDep,
    ) -> Any:
        result = await media.rename_media(session, id, body.name)
        if result is None:
            raise HTTPException(status_code=404, detail="not_found")
        return result

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
