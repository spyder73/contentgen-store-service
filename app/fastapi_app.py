from __future__ import annotations

import logging
import os
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from pydantic import BaseModel

from .db import get_session
from .logging_config import new_request_id, set_request_id, set_user_id
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
    AccessFeatureOut,
    AccessFeaturePatch,
    PipelineAssignmentsUpdate,
    PipelineRunSnapshotIn,
    PipelineRunSnapshotOut,
    PipelineTemplateIn,
    PipelineTemplateOut,
    PipelineVisibilityUpdate,
    PromptTemplateIn,
    PromptTemplateOut,
    RenameMediaBody,
    RenderProposalIn,
    RenderProposalOut,
    RenderProposalStatusPatch,
    RenderTemplateCloneIn,
    RenderTemplateIn,
    RenderTemplateOut,
    SeriesIn,
    SeriesOut,
    SwapClipMediaBody,
    ToggleFavouriteBody,
    UserAccessOut,
    UserFeatureUpdate,
    UserOut,
    VoiceSnippetOut,
)

from .stores import (
    access,
    characters,
    clips,
    credits,
    episodes,
    media,
    pipelines,
    prompts,
    render_templates,
    run_snapshots,
    series,
    system_prompts,
    users,
    voice_snippets,
)

logger = logging.getLogger(__name__)

SessionDep = Annotated[AsyncSession, Depends(get_session)]


def _get_user_id(request: Request) -> str | None:
    """Extract user_id from X-User-ID header (set by Go backend)."""
    return request.headers.get("X-User-ID") or None


def _require_user_id(request: Request) -> str:
    user_id = _get_user_id(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="user_id_required")
    return user_id


def _require_internal_secret(x_internal_secret: str | None) -> None:
    expected = os.environ.get("INTERNAL_API_SECRET", "").strip()
    if not expected:
        raise HTTPException(status_code=500, detail="internal_secret_not_configured")
    if x_internal_secret != expected:
        raise HTTPException(status_code=401, detail="invalid_internal_secret")


class CreditsReserveBody(BaseModel):
    amount_credits: int
    pipeline_run_id: str | None = None
    checkpoint_id: str | None = None
    attempt: int = 1
    idempotency_key: str


class CreditsSettleBody(BaseModel):
    pipeline_run_id: str
    checkpoint_id: str
    attempt: int
    actual_cost_usd: float
    provider: str | None = None
    model: str | None = None
    cost_source: str | None = None
    idempotency_key: str


class CreditsReleaseBody(BaseModel):
    pipeline_run_id: str
    reason: str
    idempotency_key: str


class CreditsGrantBody(BaseModel):
    user_id: str | None = None
    username: str | None = None
    amount_credits: int
    note: str = ""
    
class AuthVerifyRequest(BaseModel):
        username: str
        password: str


def _credits_error_response(err: credits.CreditsError) -> JSONResponse:
    payload: dict[str, Any] = {"error": err.code, "have": err.have, "need": err.need}
    payload.update(err.extra)
    return JSONResponse(status_code=402, content=payload)


def create_fastapi_app() -> FastAPI:
    app = FastAPI(title="contentgen-store-service")

    @app.middleware("http")
    async def _request_context(request: Request, call_next):
        import time as _t
        rid = request.headers.get("x-request-id") or new_request_id()
        set_request_id(rid)
        set_user_id(request.headers.get("x-user-id", ""))
        start = _t.perf_counter()
        try:
            response = await call_next(request)
        finally:
            elapsed_ms = int((_t.perf_counter() - start) * 1000)
            logger.info(
                "request",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "latency_ms": elapsed_ms,
                },
            )
            set_request_id("")
            set_user_id("")
        response.headers["x-request-id"] = rid
        return response

    @app.middleware("http")
    async def _internal_secret_gate(request: Request, call_next):
        # All /v1/* routes require X-Internal-Secret; /healthz and /auth/verify are public.
        if request.url.path.startswith("/v1/"):
            try:
                _require_internal_secret(request.headers.get("x-internal-secret"))
            except HTTPException as exc:
                return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
        return await call_next(request)

    # ── health ──────────────────────────────────────────────────────────────

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    # ── auth ────────────────────────────────────────────────────────────────

    @app.post("/auth/verify")
    async def verify_credentials_handler(body: AuthVerifyRequest, session: SessionDep) -> Any:
        user = await users.verify_credentials(session, body.username, body.password)
        if user is None:
            raise HTTPException(status_code=401, detail="invalid_credentials")
        return {"user_id": user.id, "username": user.username, "display_name": user.display_name}

    # ── access control ──────────────────────────────────────────────────────

    @app.get("/v1/me/access", response_model=UserAccessOut)
    async def get_my_access_handler(request: Request, session: SessionDep) -> Any:
        user_id = _require_user_id(request)
        row = await access.get_user_access(session, user_id)
        if row is None:
            raise HTTPException(status_code=404, detail="user_not_found")
        return row

    # ── pipeline templates ──────────────────────────────────────────────────

    @app.get("/v1/pipelines", response_model=list[PipelineTemplateOut])
    async def list_pipelines_handler(request: Request, session: SessionDep) -> Any:
        user_id = _get_user_id(request)
        return await pipelines.list_pipelines(session, user_id=user_id)

    @app.get("/v1/pipelines/{id}", response_model=PipelineTemplateOut)
    async def get_pipeline_handler(id: str, request: Request, session: SessionDep) -> Any:
        user_id = _require_user_id(request)
        row = await pipelines.get_pipeline(session, id, user_id=user_id)
        if row is None:
            raise HTTPException(status_code=404, detail="not_found")
        return row

    @app.put("/v1/pipelines/{id}", response_model=PipelineTemplateOut)
    async def upsert_pipeline_handler(id: str, body: PipelineTemplateIn, request: Request, session: SessionDep) -> Any:
        user_id = _require_user_id(request)
        body.id = id
        return await pipelines.upsert_pipeline(session, body, user_id=user_id)

    @app.delete("/v1/pipelines/{id}", status_code=204)
    async def delete_pipeline_handler(id: str, request: Request, session: SessionDep) -> None:
        user_id = _require_user_id(request)
        deleted = await pipelines.delete_pipeline(session, id, user_id=user_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="not_found")

    # ── pipeline run snapshots ──────────────────────────────────────────────
    # Persisted run state so the Go backend can rehydrate runs after a restart
    # (otherwise the frontend's stored run IDs 404 on regenerate).

    @app.get("/v1/run-snapshots", response_model=list[PipelineRunSnapshotOut])
    async def list_run_snapshots_handler(request: Request, session: SessionDep) -> Any:
        user_id = _get_user_id(request)
        return await run_snapshots.list_snapshots(session, user_id=user_id)

    @app.get("/v1/run-snapshots/{id}", response_model=PipelineRunSnapshotOut)
    async def get_run_snapshot_handler(id: str, session: SessionDep) -> Any:
        row = await run_snapshots.get_snapshot(session, id)
        if row is None:
            raise HTTPException(status_code=404, detail="not_found")
        return row

    @app.put("/v1/run-snapshots/{id}", response_model=PipelineRunSnapshotOut)
    async def upsert_run_snapshot_handler(
        id: str, body: PipelineRunSnapshotIn, request: Request, session: SessionDep
    ) -> Any:
        body.id = id
        return await run_snapshots.upsert_snapshot(session, body, user_id=_get_user_id(request))

    # ── prompt templates ────────────────────────────────────────────────────

    @app.get("/v1/prompts", response_model=list[PromptTemplateOut])
    async def list_prompts_handler(request: Request, session: SessionDep) -> Any:
        user_id = _get_user_id(request)
        return await prompts.list_prompts(session, user_id=user_id)

    @app.get("/v1/prompts/{id}", response_model=PromptTemplateOut)
    async def get_prompt_handler(id: str, request: Request, session: SessionDep) -> Any:
        user_id = _require_user_id(request)
        row = await prompts.get_prompt(session, id, user_id=user_id)
        if row is None:
            raise HTTPException(status_code=404, detail="not_found")
        return row

    @app.put("/v1/prompts/{id}", response_model=PromptTemplateOut)
    async def upsert_prompt_handler(id: str, body: PromptTemplateIn, request: Request, session: SessionDep) -> Any:
        user_id = _require_user_id(request)
        body.id = id
        return await prompts.upsert_prompt(session, body, user_id=user_id)

    @app.delete("/v1/prompts/{id}", status_code=204)
    async def delete_prompt_handler(id: str, request: Request, session: SessionDep) -> None:
        user_id = _require_user_id(request)
        deleted = await prompts.delete_prompt(session, id, user_id=user_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="not_found")
        return row

    @app.put("/v1/prompts/{id}", response_model=PromptTemplateOut)
    async def upsert_prompt_handler(id: str, body: PromptTemplateIn, request: Request, session: SessionDep) -> Any:
        user_id = _require_user_id(request)
        body.id = id
        return await prompts.upsert_prompt(session, body, user_id=user_id)

    @app.delete("/v1/prompts/{id}", status_code=204)
    async def delete_prompt_handler(id: str, request: Request, session: SessionDep) -> None:
        user_id = _require_user_id(request)
        deleted = await prompts.delete_prompt(session, id, user_id=user_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="not_found")

    # ── system prompts ──────────────────────────────────────────────────────

    @app.get("/v1/system-prompts/{id}", response_model=str)
    async def get_system_prompt_handler(id: str, session: SessionDep) -> Any:
        content = await system_prompts.get_system_prompt(session, id)
        if content is None:
            raise HTTPException(status_code=404, detail="not_found")
        return content

    # ── render templates ────────────────────────────────────────────────────

    @app.get("/v1/render-templates", response_model=list[RenderTemplateOut])
    async def list_render_templates_handler(
        request: Request,
        session: SessionDep,
        kind: str | None = Query(None),
        include_archived: bool = Query(False),
    ) -> Any:
        user_id = _require_user_id(request)
        return await render_templates.list_templates(
            session, user_id=user_id, kind=kind, include_archived=include_archived
        )

    @app.post("/v1/render-templates", response_model=RenderTemplateOut)
    async def create_render_template_handler(
        body: RenderTemplateIn, request: Request, session: SessionDep
    ) -> Any:
        user_id = _require_user_id(request)
        try:
            return await render_templates.create_template(session, body, user_id=user_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.get("/v1/render-templates/{id}", response_model=RenderTemplateOut)
    async def get_render_template_handler(id: str, request: Request, session: SessionDep) -> Any:
        user_id = _require_user_id(request)
        row = await render_templates.get_template(session, id, user_id=user_id)
        if row is None:
            raise HTTPException(status_code=404, detail="not_found")
        return row

    @app.put("/v1/render-templates/{id}", response_model=RenderTemplateOut)
    async def update_render_template_handler(
        id: str, body: RenderTemplateIn, request: Request, session: SessionDep
    ) -> Any:
        user_id = _require_user_id(request)
        try:
            row = await render_templates.update_template(session, id, body, user_id=user_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if row is None:
            raise HTTPException(status_code=404, detail="not_found")
        return row

    @app.delete("/v1/render-templates/{id}", status_code=204)
    async def delete_render_template_handler(id: str, request: Request, session: SessionDep) -> None:
        user_id = _require_user_id(request)
        archived = await render_templates.archive_template(session, id, user_id=user_id)
        if not archived:
            raise HTTPException(status_code=404, detail="not_found")

    @app.post("/v1/render-templates/{id}/clone", response_model=RenderTemplateOut)
    async def clone_render_template_handler(
        id: str,
        request: Request,
        session: SessionDep,
        body: RenderTemplateCloneIn | None = None,
    ) -> Any:
        user_id = _require_user_id(request)
        row = await render_templates.clone_template(
            session, id, body or RenderTemplateCloneIn(), user_id=user_id
        )
        if row is None:
            raise HTTPException(status_code=404, detail="not_found")
        return row

    # ── render proposals ────────────────────────────────────────────────────

    @app.post("/v1/render-proposals", response_model=RenderProposalOut)
    async def create_render_proposal_handler(
        body: RenderProposalIn, request: Request, session: SessionDep
    ) -> Any:
        user_id = _require_user_id(request)
        try:
            return await render_templates.create_proposal(session, body, user_id=user_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.get("/v1/render-proposals", response_model=list[RenderProposalOut])
    async def list_render_proposals_handler(
        request: Request,
        session: SessionDep,
        clip_id: str | None = Query(None),
    ) -> Any:
        user_id = _require_user_id(request)
        return await render_templates.list_proposals(session, user_id=user_id, clip_id=clip_id)

    @app.get("/v1/render-proposals/{id}", response_model=RenderProposalOut)
    async def get_render_proposal_handler(id: str, request: Request, session: SessionDep) -> Any:
        user_id = _require_user_id(request)
        row = await render_templates.get_proposal(session, id, user_id=user_id)
        if row is None:
            raise HTTPException(status_code=404, detail="not_found")
        return row

    @app.patch("/v1/render-proposals/{id}/status", response_model=RenderProposalOut)
    async def patch_render_proposal_status_handler(
        id: str, body: RenderProposalStatusPatch, request: Request, session: SessionDep
    ) -> Any:
        user_id = _require_user_id(request)
        try:
            row = await render_templates.set_proposal_status(
                session,
                id,
                body.status,
                user_id=user_id,
                validation_report_json=body.validation_report_json,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if row is None:
            raise HTTPException(status_code=404, detail="not_found")
        return row

    @app.put("/v1/system-prompts/{id}", response_model=str)
    async def upsert_system_prompt_handler(id: str, body: dict, session: SessionDep) -> Any:
        content = body.get("content", "")
        if not content:
            raise HTTPException(status_code=400, detail="content is required")
        return await system_prompts.upsert_system_prompt(session, id, content)

    # ── clips ───────────────────────────────────────────────────────────────

    @app.get("/v1/clips", response_model=PagedResponse)
    async def list_clips_handler(
        request: Request,
        session: SessionDep,
        page: int = Query(1, ge=1),
        limit: int = Query(50, ge=1, le=200),
    ) -> Any:
        user_id = _require_user_id(request)
        return await clips.list_clips(session, page=page, limit=limit, user_id=user_id)

    # NOTE: /v1/clips/summary must be registered before /v1/clips/{id}
    @app.get("/v1/clips/summary", response_model=PagedResponse)
    async def list_clip_summaries_handler(
        request: Request,
        session: SessionDep,
        page: int = Query(1, ge=1),
        limit: int = Query(50, ge=1, le=200),
        finished_only: bool = Query(False),
        search: str | None = Query(None),
    ) -> Any:
        user_id = _require_user_id(request)
        return await clips.list_clip_summaries(
            session, page=page, limit=limit, finished_only=finished_only, user_id=user_id, search=search
        )

    @app.get("/v1/clips/{id}", response_model=ClipPromptOut)
    async def get_clip_handler(id: str, session: SessionDep) -> Any:
        row = await clips.get_clip(session, id)
        if row is None:
            raise HTTPException(status_code=404, detail="not_found")
        return row

    @app.put("/v1/clips/{id}", response_model=ClipPromptOut)
    async def upsert_clip_handler(id: str, body: ClipPromptIn, request: Request, session: SessionDep) -> Any:
        body.id = id
        return await clips.upsert_clip(session, body, user_id=_get_user_id(request))

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
            raise HTTPException(status_code=404, detail=f"media_not_found: {exc}")
        except (ValueError, IndexError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if result is None:
            raise HTTPException(status_code=404, detail="clip_not_found")
        return result

    # ── media items ─────────────────────────────────────────────────────────

    # NOTE: /v1/media/{id}/file routes must be registered before /v1/media/{id}
    @app.put("/v1/media/{id}/file", status_code=204)
    async def upload_media_file_handler(id: str, request: Request, session: SessionDep) -> None:
        user_id = _require_user_id(request)
        data = await request.body()
        if not data:
            raise HTTPException(status_code=400, detail="empty body")
        mime_type = request.headers.get("Content-Type", "application/octet-stream")
        ok = await media.store_file_data(session, id, data, mime_type, user_id=user_id)
        if not ok:
            raise HTTPException(status_code=404, detail="not_found")

    @app.get("/v1/media/{id}/file")
    async def download_media_file_handler(id: str, request: Request, session: SessionDep) -> Response:
        user_id = _require_user_id(request)
        result = await media.get_file_data(session, id, user_id=user_id)
        if result is None:
            raise HTTPException(status_code=404, detail="no_file_data")
        data, mime_type = result
        ext = ""
        if "/" in mime_type:
            subtype = mime_type.split("/", 1)[1].split(";", 1)[0].strip()
            if subtype and subtype.isalnum():
                ext = "." + subtype
        return Response(
            content=data,
            media_type=mime_type,
            headers={
                "Content-Disposition": f'attachment; filename="{id}{ext}"',
                "X-Content-Type-Options": "nosniff",
            },
        )

    # NOTE: /v1/media/stats must be registered before /v1/media/{id}
    @app.get("/v1/media/stats", response_model=MediaStatsOut)
    async def get_media_stats_handler(request: Request, session: SessionDep) -> Any:
        user_id = _require_user_id(request)
        return await media.get_media_stats(session, user_id=user_id)

    @app.get("/v1/media", response_model=PagedResponse)
    async def list_media_handler(
        request: Request,
        session: SessionDep,
        clip_id: str | None = Query(None),
        type: str | None = Query(None),
        search: str | None = Query(None),
        is_favourite: bool | None = Query(None),
        pipeline_run_id: str | None = Query(None),
        scene_id: str | None = Query(None),
        role: str | None = Query(None),
        source: str | None = Query(None),
        page: int = Query(1, ge=1),
        limit: int = Query(50, ge=1, le=200),
    ) -> Any:
        user_id = _require_user_id(request)
        return await media.list_media(
            session,
            clip_id=clip_id,
            type_=type,
            search=search,
            is_favourite=is_favourite,
            pipeline_run_id=pipeline_run_id,
            scene_id=scene_id,
            role=role,
            source=source,
            page=page,
            limit=limit,
            user_id=user_id,
        )

    @app.get("/v1/media/{id}", response_model=MediaItemOut)
    async def get_media_handler(id: str, request: Request, session: SessionDep) -> Any:
        user_id = _require_user_id(request)
        row = await media.get_media(session, id, user_id=user_id)
        if row is None:
            raise HTTPException(status_code=404, detail="not_found")
        return row

    @app.put("/v1/media/{id}", response_model=MediaItemOut)
    async def upsert_media_handler(id: str, body: MediaItemIn, request: Request, session: SessionDep) -> Any:
        user_id = _require_user_id(request)
        body.id = id
        return await media.upsert_media(session, body, user_id=user_id)

    @app.delete("/v1/media/{id}", status_code=204)
    async def delete_media_handler(id: str, request: Request, session: SessionDep) -> None:
        user_id = _require_user_id(request)
        deleted = await media.delete_media(session, id, user_id=user_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="not_found")

    @app.patch("/v1/media/{id}/favourite", response_model=MediaItemOut)
    async def toggle_media_favourite(
        id: str,
        body: ToggleFavouriteBody,
        request: Request,
        session: SessionDep,
    ) -> Any:
        user_id = _require_user_id(request)
        result = await media.toggle_favourite(session, id, body.is_favourite, user_id=user_id)
        if result is None:
            raise HTTPException(status_code=404, detail="not_found")
        return result

    @app.put("/v1/media/{id}/rename", response_model=MediaItemOut)
    async def rename_media_handler(
        id: str,
        body: RenameMediaBody,
        request: Request,
        session: SessionDep,
    ) -> Any:
        user_id = _require_user_id(request)
        result = await media.rename_media(session, id, body.name, user_id=user_id)
        if result is None:
            raise HTTPException(status_code=404, detail="not_found")
        return result

    # ── series ───────────────────────────────────────────────────────────────

    @app.get("/v1/series", response_model=PagedResponse)
    async def list_series_handler(
        request: Request,
        session: SessionDep,
        page: int = Query(1, ge=1),
        limit: int = Query(50, ge=1, le=200),
    ) -> Any:
        user_id = _require_user_id(request)
        return await series.list_series(session, page=page, limit=limit, user_id=user_id)

    @app.get("/v1/series/{id}", response_model=SeriesOut)
    async def get_series_handler(id: str, session: SessionDep) -> Any:
        row = await series.get_series(session, id)
        if row is None:
            raise HTTPException(status_code=404, detail="not_found")
        return row

    @app.put("/v1/series/{id}", response_model=SeriesOut)
    async def upsert_series_handler(id: str, body: SeriesIn, request: Request, session: SessionDep) -> Any:
        body.id = id
        return await series.upsert_series(session, body, user_id=_get_user_id(request))

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

    # ── credits ──────────────────────────────────────────────────────────────

    @app.get("/v1/users/{user_id}/credits")
    async def get_user_credits_handler(user_id: str, request: Request, session: SessionDep) -> Any:
        caller = _require_user_id(request)
        if caller != user_id:
            if not await credits.is_admin(session, caller):
                raise HTTPException(status_code=403, detail="forbidden")
        bv = await credits.get_balance(session, user_id)
        if bv is None:
            raise HTTPException(status_code=404, detail="user_not_found")
        return {
            "user_id": user_id,
            "balance": bv.balance,
            "reserved": bv.reserved,
            "daily_limit": bv.daily_limit,
            "is_admin": bv.is_admin,
        }

    @app.post("/v1/users/{user_id}/credits/reserve")
    async def reserve_credits_handler(
        user_id: str,
        body: CreditsReserveBody,
        request: Request,
        session: SessionDep,
    ) -> Any:
        caller = _require_user_id(request)
        if caller != user_id:
            raise HTTPException(status_code=403, detail="forbidden")
        try:
            return await credits.reserve(
                session,
                user_id=user_id,
                amount=body.amount_credits,
                pipeline_run_id=body.pipeline_run_id,
                checkpoint_id=body.checkpoint_id,
                attempt=body.attempt,
                idempotency_key=body.idempotency_key,
            )
        except credits.CreditsError as e:
            return _credits_error_response(e)

    @app.post("/v1/users/{user_id}/credits/settle")
    async def settle_credits_handler(
        user_id: str,
        body: CreditsSettleBody,
        request: Request,
        session: SessionDep,
    ) -> Any:
        caller = _require_user_id(request)
        if caller != user_id:
            raise HTTPException(status_code=403, detail="forbidden")
        try:
            return await credits.settle(
                session,
                user_id=user_id,
                pipeline_run_id=body.pipeline_run_id,
                checkpoint_id=body.checkpoint_id,
                attempt=body.attempt,
                actual_cost_usd=body.actual_cost_usd,
                provider=body.provider,
                model=body.model,
                cost_source=body.cost_source,
                idempotency_key=body.idempotency_key,
            )
        except credits.CreditsError as e:
            return _credits_error_response(e)

    @app.post("/v1/users/{user_id}/credits/release")
    async def release_credits_handler(
        user_id: str,
        body: CreditsReleaseBody,
        request: Request,
        session: SessionDep,
    ) -> Any:
        caller = _require_user_id(request)
        if caller != user_id:
            raise HTTPException(status_code=403, detail="forbidden")
        try:
            return await credits.release(
                session,
                user_id=user_id,
                pipeline_run_id=body.pipeline_run_id,
                reason=body.reason,
                idempotency_key=body.idempotency_key,
            )
        except credits.CreditsError as e:
            return _credits_error_response(e)

    @app.post("/v1/internal/admin/credits/grant")
    async def admin_grant_credits_handler(
        body: CreditsGrantBody,
        request: Request,
        session: SessionDep,
    ) -> Any:
        admin_id = _require_user_id(request)
        if not await credits.is_admin(session, admin_id):
            raise HTTPException(status_code=403, detail="not_admin")
        target_user_id = (body.user_id or "").strip() or None
        target_username = (body.username or "").strip().lstrip("@") or None
        if not target_user_id and not target_username:
            raise HTTPException(status_code=400, detail="user_id_or_username_required")
        try:
            return await credits.grant(
                session,
                admin_user_id=admin_id,
                target_user_id=target_user_id,
                target_username=target_username,
                amount=body.amount_credits,
                note=body.note,
            )
        except credits.CreditsError as e:
            return _credits_error_response(e)

    @app.get("/v1/internal/admin/credits/ledger")
    async def admin_ledger_handler(
        request: Request,
        session: SessionDep,
        user_id: str | None = Query(None),
        username: str | None = Query(None),
        since: str | None = Query(None),
        limit: int = Query(100, ge=1, le=500),
    ) -> Any:
        admin_id = _require_user_id(request)
        if not await credits.is_admin(session, admin_id):
            raise HTTPException(status_code=403, detail="not_admin")
        username = username.strip().lstrip("@") if username else None
        return await credits.ledger(session, user_id=user_id, username=username, since=since, limit=limit)

    # ── internal admin: users, access, templates ────────────────────────────

    @app.get("/v1/internal/admin/users", response_model=list[UserOut])
    async def admin_users_handler(request: Request, session: SessionDep) -> Any:
        admin_id = _require_user_id(request)
        if not await credits.is_admin(session, admin_id):
            raise HTTPException(status_code=403, detail="not_admin")
        return await users.list_users(session)

    @app.get("/v1/internal/admin/access/features", response_model=list[AccessFeatureOut])
    async def admin_access_features_handler(request: Request, session: SessionDep) -> Any:
        admin_id = _require_user_id(request)
        if not await credits.is_admin(session, admin_id):
            raise HTTPException(status_code=403, detail="not_admin")
        return await access.list_features(session)

    @app.patch("/v1/internal/admin/access/features/{feature_key}", response_model=AccessFeatureOut)
    async def admin_patch_access_feature_handler(
        feature_key: str,
        body: AccessFeaturePatch,
        request: Request,
        session: SessionDep,
    ) -> Any:
        admin_id = _require_user_id(request)
        if not await credits.is_admin(session, admin_id):
            raise HTTPException(status_code=403, detail="not_admin")
        return await access.set_feature(session, feature_key, body.whitelist_enabled)

    @app.put("/v1/internal/admin/access/users/{user_id}/features", response_model=list[AccessFeatureOut])
    async def admin_set_user_features_handler(
        user_id: str,
        body: UserFeatureUpdate,
        request: Request,
        session: SessionDep,
    ) -> Any:
        admin_id = _require_user_id(request)
        if not await credits.is_admin(session, admin_id):
            raise HTTPException(status_code=403, detail="not_admin")
        return await access.set_user_features(session, user_id, body.feature_keys)

    @app.get("/v1/internal/admin/pipelines", response_model=list[PipelineTemplateOut])
    async def admin_list_pipelines_handler(request: Request, session: SessionDep) -> Any:
        admin_id = _require_user_id(request)
        if not await credits.is_admin(session, admin_id):
            raise HTTPException(status_code=403, detail="not_admin")
        return await pipelines.list_pipelines(session, user_id=admin_id, admin=True)

    @app.put("/v1/internal/admin/pipelines/{id}", response_model=PipelineTemplateOut)
    async def admin_upsert_pipeline_handler(
        id: str,
        body: PipelineTemplateIn,
        request: Request,
        session: SessionDep,
    ) -> Any:
        admin_id = _require_user_id(request)
        if not await credits.is_admin(session, admin_id):
            raise HTTPException(status_code=403, detail="not_admin")
        body.id = id
        return await pipelines.upsert_pipeline(session, body, user_id=admin_id, admin=True)

    @app.patch("/v1/internal/admin/pipelines/{id}/visibility", response_model=PipelineTemplateOut)
    async def admin_pipeline_visibility_handler(
        id: str,
        body: PipelineVisibilityUpdate,
        request: Request,
        session: SessionDep,
    ) -> Any:
        admin_id = _require_user_id(request)
        if not await credits.is_admin(session, admin_id):
            raise HTTPException(status_code=403, detail="not_admin")
        row = await pipelines.set_pipeline_visibility(session, id, body.visibility)
        if row is None:
            raise HTTPException(status_code=404, detail="not_found")
        return row

    @app.put("/v1/internal/admin/pipelines/{id}/assignments", response_model=PipelineTemplateOut)
    async def admin_pipeline_assignments_handler(
        id: str,
        body: PipelineAssignmentsUpdate,
        request: Request,
        session: SessionDep,
    ) -> Any:
        admin_id = _require_user_id(request)
        if not await credits.is_admin(session, admin_id):
            raise HTTPException(status_code=403, detail="not_admin")
        row = await pipelines.set_pipeline_assignments(session, id, body.user_ids)
        if row is None:
            raise HTTPException(status_code=404, detail="not_found")
        return row

    @app.delete("/v1/internal/admin/pipelines/{id}", status_code=204)
    async def admin_delete_pipeline_handler(id: str, request: Request, session: SessionDep) -> None:
        admin_id = _require_user_id(request)
        if not await credits.is_admin(session, admin_id):
            raise HTTPException(status_code=403, detail="not_admin")
        deleted = await pipelines.delete_pipeline(session, id, user_id=admin_id, admin=True)
        if not deleted:
            raise HTTPException(status_code=404, detail="not_found")

    @app.get("/v1/internal/admin/prompts", response_model=list[PromptTemplateOut])
    async def admin_list_prompts_handler(request: Request, session: SessionDep) -> Any:
        admin_id = _require_user_id(request)
        if not await credits.is_admin(session, admin_id):
            raise HTTPException(status_code=403, detail="not_admin")
        return await prompts.list_prompts(session, user_id=admin_id, admin=True)

    @app.put("/v1/internal/admin/prompts/{id}", response_model=PromptTemplateOut)
    async def admin_upsert_prompt_handler(
        id: str,
        body: PromptTemplateIn,
        request: Request,
        session: SessionDep,
    ) -> Any:
        admin_id = _require_user_id(request)
        if not await credits.is_admin(session, admin_id):
            raise HTTPException(status_code=403, detail="not_admin")
        body.id = id
        return await prompts.upsert_prompt(session, body, user_id=admin_id, admin=True)

    @app.put("/v1/admin/prompt-templates/{id}", response_model=PromptTemplateOut)
    async def admin_seed_prompt_template_handler(
        id: str,
        body: PromptTemplateIn,
        session: SessionDep,
    ) -> Any:
        """Seed a built-in prompt template if missing.

        Gated by the X-Internal-Secret middleware only (no admin user needed):
        the Go backend calls this at startup with no user context to ensure its
        local-asset prompt templates exist in the store. Create-if-missing —
        an existing row is never overwritten, so user edits to built-ins are
        preserved. Seeded rows default to global visibility.
        """
        body.id = id
        # Built-ins must resolve for every service/user, so seed them global by
        # default. The backend never sets visibility on its seed calls.
        if body.visibility != "global":
            body.visibility = "global"
        return await prompts.seed_prompt(session, body)

    # ── error handler ────────────────────────────────────────────────────────

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request, exc: Exception):
        logger.exception("unhandled error: %s", exc)
        return JSONResponse(status_code=500, content={"error": "internal_server_error"})

    return app
