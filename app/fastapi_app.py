from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_session
from .schemas import (
    ClipPromptIn,
    ClipPromptOut,
    MediaItemIn,
    MediaItemOut,
    PagedResponse,
    PipelineTemplateIn,
    PipelineTemplateOut,
    PromptTemplateIn,
    PromptTemplateOut,
)
from .stores import clips, media, pipelines, prompts

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
        page: int = Query(1, ge=1),
        limit: int = Query(50, ge=1, le=200),
    ) -> Any:
        return await media.list_media(session, clip_id=clip_id, type_=type, page=page, limit=limit)

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

    # ── error handler ────────────────────────────────────────────────────────

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request, exc: Exception):
        logger.exception("unhandled error: %s", exc)
        return JSONResponse(status_code=500, content={"error": "internal_server_error"})

    return app
