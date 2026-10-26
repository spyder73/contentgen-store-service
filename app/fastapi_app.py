from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from pydantic import BaseModel, Field, model_validator

from .db import get_session
from .logging_config import new_request_id, set_request_id, set_user_id
from .migration_state import get_migration_status
from .schemas import (
    CharacterIn,
    CharacterOut,
    ClipFullOut,
    ClipPromptIn,
    ClipPromptOut,
    ClipPromptPatch,
    ClipSummaryOut,
    DatasetTemplateCreate,
    DatasetTemplateOut,
    DatasetTemplateUpdate,
    EpisodeIn,
    EpisodeOut,
    EpisodePatch,
    GeneratorProfileCreate,
    GeneratorProfileOut,
    GeneratorProfileUpdate,
    PuppetPosePresetCreate,
    PuppetPosePresetOut,
    PuppetPosePresetUpdate,
    MediaItemIn,
    MediaItemOut,
    MediaItemPatch,
    LegacyAssetMediaOut,
    MediaRetentionIn,
    MediaRetentionOut,
    MediaStatsOut,
    PagedResponse,
    RelatedMediaOut,
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
    RenderTemplateListOut,
    RenderTemplateOut,
    RenderTemplateStatusPatch,
    SeriesIn,
    SeriesOut,
    SwapClipMediaBody,
    ToggleFavouriteBody,
    UserAccessOut,
    UserFeatureUpdate,
    UserOut,
    ClipRatingIn,
    ClipRatingOut,
    ChatApprovalApprove,
    ChatApprovalConsume,
    ChatApprovalIn,
    ChatApprovalOut,
    ChatMessageIn,
    ChatMessageOut,
    ChatMessagePatch,
    ChatThreadIn,
    ChatThreadOut,
    ChatThreadPatch,
    ChatThreadSummary,
    IdeaIn,
    IdeaOut,
    IdeaPatch,
    ReviewCorrectionIn,
    ReviewCorrectionOut,
    ReviewTraceIn,
    ReviewTraceOut,
    ClipstyleAssetOut,
    ClipstyleCatalogOut,
    ClipstylePackageMetadataPage,
    ClipstylePackageResolveOut,
    ClipstyleRenderBundleIn,
    ClipstyleRenderBundleOut,
    ClipstyleUsableOut,
    StaleHoldReapIn,
    VisibilityPatch,
)

from .stores import (
    access,
    characters,
    chat,
    clipstyle_packages,
    clipstyle_render,
    clips,
    credits,
    credits_reaper,
    credits_spent,
    episodes,
    generator_profiles,
    puppet_pose_presets,
    media,
    clip_ratings,
    ideas,
    pipelines,
    prompts,
    render_templates,
    review_corrections,
    review_traces,
    run_snapshots,
    series,
    system_prompts,
    user_settings,
    users,
    clipstyle_package_metadata,
)
from app.stores import clipstyle_assets

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


def _chat_error(exc: chat.ChatError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.message)


def _chat_found(row: Any, detail: str) -> Any:
    """Another user's row is the same 404 as a missing one."""
    if row is None:
        raise HTTPException(status_code=404, detail=detail)
    return row


_TEMPLATE_CONFLICTS = (
    "package_immutable",
    "package_kind_change_requires_draft",
    "invalid_transition",
    "package_version_already_approved",
    "package_id_taken",
    "validation_report_not_ok",
    "validation_report_stale",
    "validation_report_contract_version",
    "render_template_id_taken",
    "package_not_approved",
    "package_revoked",
    "bundle_frozen",
    "package_asset_missing",
    "media_frozen_in_clipstyle",
)


def _template_error(exc: ValueError) -> HTTPException:
    detail = str(exc)
    if detail == "validation_report_too_large":
        return HTTPException(status_code=413, detail=detail)
    return HTTPException(status_code=409 if detail.startswith(_TEMPLATE_CONFLICTS) else 400, detail=detail)


def _visibility_error(exc: ValueError) -> HTTPException:
    """Shared by the owner's and the admin's visibility routes (review round 1, M1):
    invalid_visibility is a 422 from the handler itself; every other refusal (package_not_approved,
    package_revoked, not_found-shaped ValueErrors) goes through _template_error as usual."""
    if str(exc) == "invalid_visibility":
        return HTTPException(status_code=422, detail="invalid_visibility")
    return _template_error(exc)


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
    # Both set: release only that one hold. Both absent: every open hold of the
    # run (what older backends send). One without the other is rejected rather
    # than silently widened to a run-wide release or guessed.
    checkpoint_id: str | None = None
    attempt: int | None = None

    @model_validator(mode="after")
    def _hold_key_complete(self) -> "CreditsReleaseBody":
        if (self.checkpoint_id is None) != (self.attempt is None):
            raise ValueError("checkpoint_id and attempt must be sent together")
        return self


class CreditsSpentBody(BaseModel):
    # {scope name: ledger keys}. The backend sends one scope per clip: the
    # clip id plus the pipeline runs that produced it. Bounded so one list
    # request stays one cheap indexed query.
    scopes: dict[str, list[str]] = Field(default_factory=dict, max_length=500)

    @model_validator(mode="after")
    def _bounded_keys(self) -> "CreditsSpentBody":
        if any(len(keys) > 64 for keys in self.scopes.values()):
            raise ValueError("at most 64 ledger keys per scope")
        return self


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
        return {"status": "ok", "migrations": get_migration_status()}

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
        try:
            return await pipelines.upsert_pipeline(session, body, user_id=user_id)
        except pipelines.PipelineTemplateError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.message)

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

    @app.delete("/v1/run-snapshots/{id}", status_code=204)
    async def delete_run_snapshot_handler(id: str, session: SessionDep) -> None:
        await run_snapshots.delete_snapshot(session, id)

    @app.delete("/v1/run-snapshots")
    async def delete_all_run_snapshots_handler(session: SessionDep) -> Any:
        deleted = await run_snapshots.delete_all_snapshots(session)
        return {"deleted": deleted}

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
        try:
            return await prompts.upsert_prompt(session, body, user_id=user_id)
        except prompts.PromptTemplateError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.message)

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

    @app.get("/v1/render-templates", response_model=list[RenderTemplateListOut])
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
            raise _template_error(exc)

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
            raise _template_error(exc)
        if row is None:
            raise HTTPException(status_code=404, detail="not_found")
        return row

    @app.delete("/v1/render-templates/{id}", status_code=204)
    async def delete_render_template_handler(id: str, request: Request, session: SessionDep) -> None:
        user_id = _require_user_id(request)
        try:
            archived = await render_templates.archive_template(session, id, user_id=user_id)
        except ValueError as exc:
            raise _template_error(exc)
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
        try:
            row = await render_templates.clone_template(
                session, id, body or RenderTemplateCloneIn(), user_id=user_id
            )
        except ValueError as exc:
            raise _template_error(exc)
        if row is None:
            raise HTTPException(status_code=404, detail="not_found")
        return row

    @app.patch("/v1/render-templates/{id}/status", response_model=RenderTemplateOut)
    async def patch_render_template_status_handler(
        id: str, body: RenderTemplateStatusPatch, request: Request, session: SessionDep
    ) -> Any:
        user_id = _require_user_id(request)
        try:
            row = await clipstyle_packages.set_package_status(
                session,
                id,
                body.status,
                user_id=user_id,
                validation_report_json=body.validation_report_json,
                preview_url=body.preview_url,
            )
        except ValueError as exc:
            raise _template_error(exc)
        if row is None:
            raise HTTPException(status_code=404, detail="not_found")
        return row

    @app.patch("/v1/render-templates/{id}/visibility")
    async def set_render_template_visibility_handler(id: str, body: VisibilityPatch, request: Request, session: SessionDep) -> Any:
        # The owner's route (design § Visibility and publishing, P4R-46): toggles private <-> published
        # of their own approved version, never a revoked one. Two routes, two jobs: an admin uses the
        # internal/admin route below instead.
        owner = _require_user_id(request)
        try:
            out = await clipstyle_render.set_visibility(session, id, body.visibility, owner=owner)
        except ValueError as exc:
            raise _visibility_error(exc) from exc
        if out is None:
            raise HTTPException(status_code=404, detail="not_found")
        return out

    @app.patch("/v1/internal/admin/render-templates/{id}/visibility")
    async def admin_set_render_template_visibility_handler(id: str, body: VisibilityPatch, request: Request, session: SessionDep) -> Any:
        # The admin's route (review round 1, I-1): same shape as its sibling admin routes, e.g.
        # admin_pipeline_visibility_handler — internal-secret gated (middleware) AND the caller must
        # be a verified admin (credits.is_admin), not merely any holder of the shared secret. Sets
        # private (unpublish, or lift a revoke) or revoked; never published — only the owner publishes.
        admin_id = _require_user_id(request)
        if not await credits.is_admin(session, admin_id):
            raise HTTPException(status_code=403, detail="not_admin")
        try:
            out = await clipstyle_render.set_visibility(session, id, body.visibility, admin=True)
        except ValueError as exc:
            raise _visibility_error(exc) from exc
        if out is None:
            raise HTTPException(status_code=404, detail="not_found")
        return out

    @app.get("/v1/internal/clipstyle-packages/approved", response_model=list[RenderTemplateOut])
    async def list_approved_clipstyle_packages_handler(session: SessionDep) -> Any:
        # Internal-secret gate only (middleware), across all users: the bridge re-registers every
        # approved package after a registry restart (the registry keeps styles in memory).
        return await clipstyle_packages.list_approved_packages(session)

    @app.post("/v1/internal/clipstyle-packages/{id}/bundles", response_model=ClipstyleRenderBundleOut)
    async def add_clipstyle_render_bundle_handler(id: str, body: ClipstyleRenderBundleIn, session: SessionDep) -> Any:
        # Internal-secret gate only (middleware): the bridge freezes the render bundle of a version
        # it just approved (or backfills one on a new image), whoever owns the package.
        try:
            row = await clipstyle_render.add_bundle(session, id, body)
        except ValueError as exc:
            raise _template_error(exc)
        if row is None:
            raise HTTPException(status_code=404, detail="not_found")
        return row

    @app.get("/v1/internal/clipstyle-packages/{id}/bundles", response_model=list[ClipstyleRenderBundleOut])
    async def list_clipstyle_render_bundles_handler(id: str, session: SessionDep) -> Any:
        return await clipstyle_render.list_bundles(session, id)

    @app.get("/v1/internal/clipstyle-packages/{id}/assets", response_model=list[ClipstyleAssetOut])
    async def list_clipstyle_assets_handler(id: str, session: SessionDep) -> Any:
        return await clipstyle_assets.list_assets(session, id)

    @app.get("/v1/internal/clipstyle-packages/catalog", response_model=list[ClipstyleCatalogOut])
    async def clipstyle_package_catalog_handler(session: SessionDep) -> Any:
        # Internal-secret gate only (middleware): the admin's catalog of every approved package
        # version, across all users (design § Visibility and publishing). No package files.
        return await clipstyle_render.list_catalog(session)

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

    # ── clipstyle packages ──────────────────────────────────────────────────

    @app.get("/v1/clipstyle-packages/resolve", response_model=ClipstylePackageResolveOut)
    async def resolve_clipstyle_package_handler(
        request: Request,
        session: SessionDep,
        style: str = Query(..., pattern=r"^[a-z][A-Za-z0-9]{2,47}$"),
        version: int | None = Query(None, ge=1),
    ) -> Any:
        """Phase 4: is `style@version` an approved package row, and what does the caller need to
        know to decide access and routing. Facts only; the backend decides."""
        caller = _require_user_id(request)
        row = await clipstyle_render.resolve_package(session, style=style, version=version, caller=caller)
        if row is None:
            raise HTTPException(status_code=404, detail="not_a_package")
        return row

    @app.get("/v1/clipstyle-packages/usable", response_model=list[ClipstyleUsableOut])
    async def list_usable_clipstyle_packages_handler(request: Request, session: SessionDep) -> Any:
        # Registered before any /v1/clipstyle-packages/{id} route, so "usable" is never captured as
        # an id. The caller's own unrevoked approved versions, plus everyone's published ones.
        return await clipstyle_render.list_usable(session, caller=_require_user_id(request))

    @app.get("/v1/clipstyle-packages/metadata", response_model=ClipstylePackageMetadataPage)
    async def list_clipstyle_package_metadata_handler(
        request: Request,
        session: SessionDep,
        include_archived: bool = Query(False),
        page: int = Query(1, ge=1, le=clipstyle_package_metadata.MAX_PAGE),
        limit: int = Query(clipstyle_package_metadata.DEFAULT_LIMIT, ge=1, le=clipstyle_package_metadata.MAX_LIMIT),
    ) -> Any:
        """The caller's own clipstyle package rows, without their package files, a page at a time."""
        user_id = _require_user_id(request)
        return await clipstyle_package_metadata.list_package_metadata(
            session, user_id=user_id, include_archived=include_archived, page=page, limit=limit
        )

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
        unfinished_only: bool = Query(False),
        search: str | None = Query(None),
    ) -> Any:
        user_id = _require_user_id(request)
        return await clips.list_clip_summaries(
            session,
            page=page,
            limit=limit,
            finished_only=finished_only,
            unfinished_only=unfinished_only,
            user_id=user_id,
            search=search,
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

    @app.patch("/v1/clips/{id}", response_model=ClipPromptOut)
    async def patch_clip_handler(id: str, body: ClipPromptPatch, session: SessionDep) -> Any:
        # exclude_unset, not the model's values: an absent key must leave its
        # column untouched (unlike the PUT above, which assigns every column
        # unconditionally). An explicit `null` in the body still clears a
        # column — it IS present in model_fields_set, just with value None.
        fields = body.model_dump(exclude_unset=True)
        if not fields:
            raise HTTPException(status_code=400, detail="empty_body")
        row = await clips.patch_clip(session, id, fields)
        if row is None:
            raise HTTPException(status_code=404, detail="not_found")
        return row

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
        try:
            ok = await media.store_file_data(session, id, data, mime_type, user_id=user_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if not ok:
            raise HTTPException(status_code=404, detail="not_found")

    @app.get("/v1/media/{id}/file")
    async def download_media_file_handler(id: str, request: Request, session: SessionDep) -> Response:
        # No _require_user_id: this route is reached via the go-backend's
        # public, unauthenticated /media/uploads embed path, which forwards
        # X-Internal-Secret but no X-User-ID. The internal-secret gate (see
        # _internal_secret_gate middleware) is the auth; the UUID id is the
        # access gate for the bytes. X-User-ID, when present, is still
        # honored as an extra ownership filter (see media.get_file_data).
        user_id = _get_user_id(request)
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

    # NOTE: /v1/media/{id}/thumbnail must be registered before /v1/media/{id}
    @app.get("/v1/media/{id}/thumbnail")
    async def download_media_thumbnail_handler(
        id: str, request: Request, session: SessionDep
    ) -> Response:
        # See download_media_file_handler: no _require_user_id — this route
        # is reached via the go-backend's public embed path with no
        # X-User-ID, gated by X-Internal-Secret instead.
        user_id = _get_user_id(request)
        result = await media.get_thumbnail(session, id, user_id=user_id)
        if result is None:
            # No derivative (non-image, missing bytes, or undecodable) — 404 so
            # the proxy falls back to serving the original.
            raise HTTPException(status_code=404, detail="no_thumbnail")
        data, content_type = result
        return Response(
            content=data,
            media_type=content_type,
            headers={
                "Cache-Control": "public, max-age=86400",
                "X-Content-Type-Options": "nosniff",
            },
        )

    # NOTE: /v1/media/{id}/proxy must be registered before /v1/media/{id}
    @app.put("/v1/media/{id}/proxy", status_code=204)
    async def upload_media_proxy_handler(id: str, request: Request, session: SessionDep) -> None:
        # No _require_user_id: the Go backend uploads the transcoded proxy as a
        # best-effort cache-fill (see download_media_proxy_handler / D5), which
        # may run from a background singleflight fill with no request-scoped
        # user context. X-User-ID, when present, is still honored as an extra
        # ownership filter (see media.store_proxy_data).
        user_id = _get_user_id(request)
        data = await request.body()
        if not data:
            raise HTTPException(status_code=400, detail="empty body")
        mime_type = request.headers.get("Content-Type", "video/mp4")
        ok = await media.store_proxy_data(session, id, data, mime_type, user_id=user_id)
        if not ok:
            raise HTTPException(status_code=404, detail="not_found")

    @app.get("/v1/media/{id}/proxy")
    async def download_media_proxy_handler(id: str, request: Request, session: SessionDep) -> Response:
        # See download_media_thumbnail_handler: no _require_user_id — this
        # route is reached via the go-backend's public embed path with no
        # X-User-ID, gated by X-Internal-Secret instead.
        user_id = _get_user_id(request)
        result = await media.get_proxy(session, id, user_id=user_id)
        if result is None:
            raise HTTPException(status_code=404, detail="no_proxy")
        data, content_type = result
        return Response(
            content=data,
            media_type=content_type,
            headers={
                "Cache-Control": "public, max-age=86400",
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
        generator_profile_id: str | None = Query(None),
        sort: Literal["newest", "oldest", "favourite", "name"] = Query("newest"),
        created_after: datetime | None = Query(None),
        has_controlnet: bool | None = Query(None),
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
            generator_profile_id=generator_profile_id,
            sort=sort,
            created_after=created_after,
            has_controlnet=has_controlnet,
            page=page,
            limit=limit,
            user_id=user_id,
        )

    # NOTE: /v1/media/{id}/related must be registered before /v1/media/{id}
    @app.get("/v1/media/{id}/related", response_model=RelatedMediaOut)
    async def get_related_media_handler(id: str, request: Request, session: SessionDep) -> Any:
        user_id = _require_user_id(request)
        related = await media.get_related_media(session, id, user_id=user_id)
        if related is None:
            raise HTTPException(status_code=404, detail="not_found")
        return related

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
        try:
            result = await media.upsert_media(session, body, user_id=user_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        # None (M7 fix round 2): a frozen id that isn't the caller's — refused like any other media
        # they don't own, never revealing that it's frozen.
        if result is None:
            raise HTTPException(status_code=404, detail="not_found")
        return result

    @app.patch("/v1/media/{id}", response_model=MediaItemOut)
    async def patch_media_handler(
        id: str,
        body: MediaItemPatch,
        request: Request,
        session: SessionDep,
    ) -> Any:
        user_id = _require_user_id(request)
        try:
            result = await media.patch_media(session, id, body, user_id=user_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if result is None:
            raise HTTPException(status_code=404, detail="not_found")
        return result

    @app.delete("/v1/media/{id}", status_code=204)
    async def delete_media_handler(id: str, request: Request, session: SessionDep) -> None:
        user_id = _require_user_id(request)
        try:
            deleted = await media.delete_media(session, id, user_id=user_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
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

    # ── maintenance ──────────────────────────────────────────────────────────

    @app.get("/v1/maintenance/legacy-asset-media", response_model=list[LegacyAssetMediaOut])
    async def legacy_asset_media_handler(
        session: SessionDep,
        limit: int = Query(default=500, ge=1, le=2000),
    ) -> Any:
        """Media rows whose bytes still live only in the backend's scratch dir.

        Cross-user on purpose: the backend migrates these into the durable
        upload path at boot, before its own sweep can delete the files.
        """
        return await media.list_legacy_asset_media(session, limit=limit)

    @app.post("/v1/maintenance/media-retention", response_model=MediaRetentionOut)
    async def media_retention_handler(
        session: SessionDep,
        body: MediaRetentionIn | None = None,
        older_than_days: int | None = Query(default=None, ge=1),
        dry_run: bool | None = Query(default=None),
    ) -> Any:
        """Drop the reproducible bytes of rendered clips older than the window.

        Accepts the window either as a JSON body or as query params so the
        nightly Go caller can POST with no payload; an explicit query param wins
        over the body. No user scoping: this is a host-wide maintenance sweep
        run by the backend, behind the same X-Internal-Secret gate as every
        other /v1 route.
        """
        days = older_than_days if older_than_days is not None else (
            body.older_than_days if body is not None else None
        )
        # Both entry points constrain the value to >= 1; only its absence is
        # left to check here.
        if days is None:
            raise HTTPException(status_code=422, detail="older_than_days_required")
        # Absent everywhere => dry run, so a call that forgets the flag reports
        # instead of erasing.
        is_dry = dry_run if dry_run is not None else (body.dry_run if body is not None else True)
        return await media.sweep_media_retention(
            session, older_than_days=days, dry_run=is_dry
        )

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
    async def get_series_handler(id: str, request: Request, session: SessionDep) -> Any:
        row = await series.get_series(session, id, user_id=_require_user_id(request))
        if row is None:
            raise HTTPException(status_code=404, detail="not_found")
        return row

    @app.put("/v1/series/{id}", response_model=SeriesOut)
    async def upsert_series_handler(id: str, body: SeriesIn, request: Request, session: SessionDep) -> Any:
        body.id = id
        try:
            return await series.upsert_series(session, body, user_id=_get_user_id(request))
        except series.SeriesError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.message)

    @app.delete("/v1/series/{id}", status_code=204)
    async def delete_series_handler(id: str, request: Request, session: SessionDep) -> None:
        deleted = await series.delete_series(session, id, user_id=_require_user_id(request))
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
    async def get_character_handler(id: str, request: Request, session: SessionDep) -> Any:
        row = await characters.get_character(session, id, user_id=_require_user_id(request))
        if row is None:
            raise HTTPException(status_code=404, detail="not_found")
        return row

    @app.put("/v1/characters/{id}", response_model=CharacterOut)
    async def upsert_character_handler(id: str, body: CharacterIn, request: Request, session: SessionDep) -> Any:
        body.id = id
        try:
            return await characters.upsert_character(session, body, user_id=_get_user_id(request))
        except series.SeriesError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.message)

    @app.delete("/v1/characters/{id}", status_code=204)
    async def delete_character_handler(id: str, request: Request, session: SessionDep) -> None:
        deleted = await characters.delete_character(session, id, user_id=_require_user_id(request))
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
    async def get_episode_handler(id: str, request: Request, session: SessionDep) -> Any:
        row = await episodes.get_episode(session, id, user_id=_require_user_id(request))
        if row is None:
            raise HTTPException(status_code=404, detail="not_found")
        return row

    @app.put("/v1/episodes/{id}", response_model=EpisodeOut)
    async def upsert_episode_handler(id: str, body: EpisodeIn, session: SessionDep) -> Any:
        body.id = id
        return await episodes.upsert_episode(session, body)

    @app.patch("/v1/episodes/{id}", response_model=EpisodeOut)
    async def patch_episode_handler(
        id: str, body: EpisodePatch, request: Request, session: SessionDep
    ) -> Any:
        # Partial update: only the keys in the body are written. Owner-scoped,
        # so an episode belonging to another tenant answers 404 like a missing
        # one — the recorder never needs to write outside its own series.
        row = await episodes.patch_episode(
            session, id, body, user_id=_require_user_id(request)
        )
        if row is None:
            raise HTTPException(status_code=404, detail="not_found")
        return row

    @app.delete("/v1/episodes/{id}", status_code=204)
    async def delete_episode_handler(id: str, request: Request, session: SessionDep) -> None:
        deleted = await episodes.delete_episode(session, id, user_id=_require_user_id(request))
        if not deleted:
            raise HTTPException(status_code=404, detail="not_found")

    # ── generator profiles ───────────────────────────────────────────────────
    # Versioned generator definitions (base model + adapters + prompt + params).
    # A slug groups versions; a slug holds at most one draft. Publishing freezes
    # a row (immutable, undeletable); drafts are the only mutable/deletable rows.

    @app.get("/v1/generator-profiles", response_model=list[GeneratorProfileOut])
    async def list_generator_profiles_handler(
        session: SessionDep,
        status: str | None = Query(None),
        user_id: str | None = Query(None),
        slug: str | None = Query(None),
    ) -> Any:
        return await generator_profiles.list_profiles(
            session, status=status, user_id=user_id, slug=slug
        )

    # NOTE: /v1/generator-profiles/by-ref/{ref} must precede /{id}.
    @app.get("/v1/generator-profiles/by-ref/{ref}", response_model=GeneratorProfileOut)
    async def get_generator_profile_by_ref_handler(ref: str, session: SessionDep) -> Any:
        try:
            row = await generator_profiles.get_profile_by_ref(session, ref)
        except generator_profiles.GeneratorProfileError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.message)
        if row is None:
            raise HTTPException(status_code=404, detail="not_found")
        return row

    @app.get("/v1/generator-profiles/{id}", response_model=GeneratorProfileOut)
    async def get_generator_profile_handler(id: str, session: SessionDep) -> Any:
        row = await generator_profiles.get_profile(session, id)
        if row is None:
            raise HTTPException(status_code=404, detail="not_found")
        return row

    @app.post("/v1/generator-profiles", response_model=GeneratorProfileOut)
    async def create_generator_profile_handler(
        body: GeneratorProfileCreate, session: SessionDep
    ) -> Any:
        try:
            return await generator_profiles.create_profile(session, body)
        except generator_profiles.GeneratorProfileError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.message)

    @app.put("/v1/generator-profiles/{id}", response_model=GeneratorProfileOut)
    async def update_generator_profile_handler(
        id: str, body: GeneratorProfileUpdate, session: SessionDep
    ) -> Any:
        try:
            row = await generator_profiles.update_profile(session, id, body)
        except generator_profiles.GeneratorProfileError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.message)
        if row is None:
            raise HTTPException(status_code=404, detail="not_found")
        return row

    @app.post("/v1/generator-profiles/{id}/publish", response_model=GeneratorProfileOut)
    async def publish_generator_profile_handler(id: str, session: SessionDep) -> Any:
        try:
            row = await generator_profiles.publish_profile(session, id)
        except generator_profiles.GeneratorProfileError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.message)
        if row is None:
            raise HTTPException(status_code=404, detail="not_found")
        return row

    @app.delete("/v1/generator-profiles/{id}", status_code=204)
    async def delete_generator_profile_handler(id: str, session: SessionDep) -> None:
        try:
            deleted = await generator_profiles.delete_profile(session, id)
        except generator_profiles.GeneratorProfileError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.message)
        if deleted is None:
            raise HTTPException(status_code=404, detail="not_found")

    # ── user Puppet pose library ────────────────────────────────────────────

    @app.get("/v1/puppet-poses", response_model=list[PuppetPosePresetOut])
    async def list_puppet_poses_handler(request: Request, session: SessionDep) -> Any:
        return await puppet_pose_presets.list_presets(session, _require_user_id(request))

    @app.get("/v1/puppet-poses/{id}", response_model=PuppetPosePresetOut)
    async def get_puppet_pose_handler(id: str, request: Request, session: SessionDep) -> Any:
        row = await puppet_pose_presets.get_preset(session, id, _require_user_id(request))
        if row is None:
            raise HTTPException(status_code=404, detail="not_found")
        return row

    @app.post("/v1/puppet-poses", response_model=PuppetPosePresetOut)
    async def create_puppet_pose_handler(
        body: PuppetPosePresetCreate, request: Request, session: SessionDep
    ) -> Any:
        try:
            return await puppet_pose_presets.create_preset(session, _require_user_id(request), body)
        except puppet_pose_presets.PuppetPosePresetConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    @app.put("/v1/puppet-poses/{id}", response_model=PuppetPosePresetOut)
    async def update_puppet_pose_handler(
        id: str, body: PuppetPosePresetUpdate, request: Request, session: SessionDep
    ) -> Any:
        try:
            row = await puppet_pose_presets.update_preset(session, id, _require_user_id(request), body)
        except puppet_pose_presets.PuppetPosePresetConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        if row is None:
            raise HTTPException(status_code=404, detail="not_found")
        return row

    @app.delete("/v1/puppet-poses/{id}", status_code=204)
    async def delete_puppet_pose_handler(id: str, request: Request, session: SessionDep) -> None:
        if not await puppet_pose_presets.delete_preset(session, id, _require_user_id(request)):
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
                checkpoint_id=body.checkpoint_id,
                attempt=body.attempt,
            )
        except credits.CreditsError as e:
            return _credits_error_response(e)

    @app.post("/v1/users/{user_id}/credits/spent")
    async def spent_credits_handler(
        user_id: str,
        body: CreditsSpentBody,
        request: Request,
        session: SessionDep,
    ) -> Any:
        """Debited credits (markup included) and raw USD per scope of ledger
        keys — what the user paid, read from the ledger, never re-priced."""
        caller = _require_user_id(request)
        if caller != user_id:
            raise HTTPException(status_code=403, detail="forbidden")
        return {"scopes": await credits_spent.spent_by_scope(session, user_id, body.scopes)}

    @app.patch("/v1/users/{user_id}/limits")
    async def set_user_daily_limit_handler(
        user_id: str, session: SessionDep, body: Any = Body(default=None)
    ) -> Any:
        try:
            return await users.set_daily_spend_limit(session, user_id, body)
        except users.DailyLimitError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.message)

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

    @app.post("/v1/internal/credits/reap-stale-holds")
    async def reap_stale_holds_handler(body: StaleHoldReapIn, session: SessionDep) -> Any:
        # Internal-secret gated only (middleware above): a backend cron caller, not a user action.
        holds = await credits_reaper.reap_stale_holds(session, [r.model_dump() for r in body.rules],
                                                      now=datetime.now(timezone.utc), limit=body.limit)
        return {"released": len(holds), "holds": holds}

    # ── idea library ─────────────────────────────────────────────────────────
    #
    # Ideas are captured silently at run start (seed) and patched at finalize
    # (refined); the verdict lives on the clip, where judgment happens, and
    # links back via a nullable idea_id resolved at rating time.

    @app.post("/v1/ideas", response_model=IdeaOut)
    async def create_idea_handler(payload: IdeaIn, request: Request, session: SessionDep) -> Any:
        user_id = _require_user_id(request)
        row = await ideas.create_idea(session, user_id, payload)
        return IdeaOut.model_validate(row)

    @app.get("/v1/ideas", response_model=list[IdeaOut])
    async def list_ideas_handler(
        request: Request,
        session: SessionDep,
        template_id: str | None = None,
        run_id: str | None = None,
        min_score: float | None = None,
        limit: int = 50,
    ) -> Any:
        user_id = _require_user_id(request)
        return await ideas.list_ideas(
            session, user_id, template_id=template_id, run_id=run_id,
            min_score=min_score, limit=max(1, min(limit, 200)),
        )

    @app.get("/v1/ideas/{idea_id}", response_model=IdeaOut)
    async def get_idea_handler(idea_id: str, request: Request, session: SessionDep) -> Any:
        user_id = _require_user_id(request)
        row = await ideas.get_idea(session, idea_id, user_id)
        if row is None:
            raise HTTPException(status_code=404, detail="idea_not_found")
        return IdeaOut.model_validate(row)

    @app.patch("/v1/ideas/{idea_id}", response_model=IdeaOut)
    async def patch_idea_handler(
        idea_id: str, payload: IdeaPatch, request: Request, session: SessionDep
    ) -> Any:
        user_id = _require_user_id(request)
        row = await ideas.patch_refined(session, idea_id, user_id, payload.refined)
        if row is None:
            raise HTTPException(status_code=404, detail="idea_not_found")
        return IdeaOut.model_validate(row)

    @app.delete("/v1/ideas/{idea_id}", status_code=204)
    async def delete_idea_handler(idea_id: str, request: Request, session: SessionDep) -> Response:
        user_id = _require_user_id(request)
        deleted = await ideas.delete_idea(session, idea_id, user_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="idea_not_found")
        return Response(status_code=204)

    @app.post("/v1/clip-ratings", response_model=ClipRatingOut)
    async def upsert_clip_rating_handler(
        payload: ClipRatingIn, request: Request, session: SessionDep
    ) -> Any:
        user_id = _require_user_id(request)
        try:
            row = await clip_ratings.upsert_rating(session, user_id, payload)
        except clip_ratings.ClipRatingError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.message)
        return ClipRatingOut.model_validate(row)

    @app.get("/v1/clip-ratings", response_model=ClipRatingOut | None)
    async def get_clip_rating_handler(clip_id: str, request: Request, session: SessionDep) -> Any:
        user_id = _require_user_id(request)
        row = await clip_ratings.get_rating(session, user_id, clip_id)
        return ClipRatingOut.model_validate(row) if row is not None else None

    # ── reviewer trace log + corrections ────────────────────────────────────
    #
    # review_traces is an append-only log of every check-tier call the
    # reviewer made (id minted by the Go backend so a retried POST is
    # idempotent); review_corrections is the much smaller table of human
    # rulings on a verdict, upserted on (user_id, verdict_id).

    @app.post("/v1/review-traces", response_model=ReviewTraceOut)
    async def create_review_trace_handler(
        payload: ReviewTraceIn, request: Request, session: SessionDep
    ) -> Any:
        user_id = _require_user_id(request)
        try:
            row = await review_traces.create_trace(session, user_id, payload)
        except review_traces.ReviewTraceError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.message)
        return ReviewTraceOut.model_validate(row)

    @app.get("/v1/review-traces", response_model=list[ReviewTraceOut])
    async def list_review_traces_handler(
        request: Request, session: SessionDep, run_id: str, limit: int = 200, offset: int = 0
    ) -> Any:
        user_id = _require_user_id(request)
        return await review_traces.list_traces(
            session, user_id, run_id=run_id, limit=max(1, min(limit, 200)), offset=max(0, offset)
        )

    @app.post("/v1/review-corrections", response_model=ReviewCorrectionOut)
    async def upsert_review_correction_handler(
        payload: ReviewCorrectionIn, request: Request, session: SessionDep
    ) -> Any:
        user_id = _require_user_id(request)
        try:
            row = await review_corrections.upsert_correction(session, user_id, payload)
        except review_corrections.ReviewCorrectionError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.message)
        return ReviewCorrectionOut.model_validate(row)

    @app.get("/v1/review-corrections", response_model=list[ReviewCorrectionOut])
    async def list_review_corrections_handler(
        request: Request,
        session: SessionDep,
        template_id: str | None = None,
        label: str | None = None,
        limit: int = 50,
    ) -> Any:
        user_id = _require_user_id(request)
        return await review_corrections.list_corrections(
            session, user_id, template_id=template_id, label=label,
            limit=max(1, min(limit, 200)),
        )

    # ── operator chat ────────────────────────────────────────────────────────
    #
    # Threads, their messages and the one-time spend approvals the operator's
    # spend tools consume. Everything is scoped by X-User-ID; another user's
    # row is a 404. token_sha256 goes in (approve, consume) and never comes out.

    @app.post("/v1/chat/threads", response_model=ChatThreadOut)
    async def create_chat_thread_handler(payload: ChatThreadIn, request: Request, session: SessionDep) -> Any:
        user_id = _require_user_id(request)
        try:
            return await chat.create_thread(session, user_id, payload)
        except chat.ChatError as exc:
            raise _chat_error(exc)

    @app.get("/v1/chat/threads", response_model=list[ChatThreadSummary])
    async def list_chat_threads_handler(
        request: Request, session: SessionDep, limit: int = 100, offset: int = 0
    ) -> Any:
        return await chat.list_threads(session, _require_user_id(request), limit=limit, offset=offset)

    @app.get("/v1/chat/threads/{thread_id}", response_model=ChatThreadOut)
    async def get_chat_thread_handler(thread_id: str, request: Request, session: SessionDep) -> Any:
        row = await chat.get_thread(session, _require_user_id(request), thread_id)
        return _chat_found(row, "thread_not_found")

    @app.patch("/v1/chat/threads/{thread_id}", response_model=ChatThreadOut)
    async def patch_chat_thread_handler(
        thread_id: str, payload: ChatThreadPatch, request: Request, session: SessionDep
    ) -> Any:
        row = await chat.patch_thread(session, _require_user_id(request), thread_id, payload)
        return _chat_found(row, "thread_not_found")

    @app.delete("/v1/chat/threads/{thread_id}", status_code=204)
    async def delete_chat_thread_handler(thread_id: str, request: Request, session: SessionDep) -> Response:
        if not await chat.delete_thread(session, _require_user_id(request), thread_id):
            raise HTTPException(status_code=404, detail="thread_not_found")
        return Response(status_code=204)

    @app.post("/v1/chat/threads/{thread_id}/messages", response_model=ChatMessageOut)
    async def create_chat_message_handler(
        thread_id: str, payload: ChatMessageIn, request: Request, session: SessionDep
    ) -> Any:
        try:
            row = await chat.create_message(session, _require_user_id(request), thread_id, payload)
        except chat.ChatError as exc:
            raise _chat_error(exc)
        return _chat_found(row, "thread_not_found")

    @app.get("/v1/chat/threads/{thread_id}/messages", response_model=list[ChatMessageOut])
    async def list_chat_messages_handler(
        thread_id: str, request: Request, session: SessionDep, after_seq: int = 0, limit: int = 500
    ) -> Any:
        rows = await chat.list_messages(
            session, _require_user_id(request), thread_id, after_seq=after_seq, limit=limit
        )
        return _chat_found(rows, "thread_not_found")

    @app.patch("/v1/chat/messages/{message_id}", response_model=ChatMessageOut)
    async def patch_chat_message_handler(
        message_id: str, payload: ChatMessagePatch, request: Request, session: SessionDep
    ) -> Any:
        row = await chat.patch_message(session, _require_user_id(request), message_id, payload)
        return _chat_found(row, "message_not_found")

    @app.post("/v1/chat/threads/{thread_id}/approvals", response_model=ChatApprovalOut)
    async def create_chat_approval_handler(
        thread_id: str, payload: ChatApprovalIn, request: Request, session: SessionDep
    ) -> Any:
        try:
            row = await chat.create_approval(session, _require_user_id(request), thread_id, payload)
        except chat.ChatError as exc:
            raise _chat_error(exc)
        return _chat_found(row, "thread_not_found")

    @app.post("/v1/chat/approvals/consume", response_model=ChatApprovalOut)
    async def consume_chat_approval_handler(
        payload: ChatApprovalConsume, request: Request, session: SessionDep
    ) -> Any:
        try:
            return await chat.consume_approval(session, _require_user_id(request), payload)
        except chat.ChatError as exc:
            raise _chat_error(exc)

    @app.get("/v1/chat/approvals/{approval_id}", response_model=ChatApprovalOut)
    async def get_chat_approval_handler(approval_id: str, request: Request, session: SessionDep) -> Any:
        row = await chat.get_approval(session, _require_user_id(request), approval_id)
        return _chat_found(row, "approval_not_found")

    @app.post("/v1/chat/approvals/{approval_id}/approve", response_model=ChatApprovalOut)
    async def approve_chat_approval_handler(
        approval_id: str, payload: ChatApprovalApprove, request: Request, session: SessionDep
    ) -> Any:
        try:
            row = await chat.approve_approval(session, _require_user_id(request), approval_id, payload)
        except chat.ChatError as exc:
            raise _chat_error(exc)
        return _chat_found(row, "approval_not_found")

    @app.post("/v1/chat/approvals/{approval_id}/decline", response_model=ChatApprovalOut)
    async def decline_chat_approval_handler(approval_id: str, request: Request, session: SessionDep) -> Any:
        try:
            row = await chat.decline_approval(session, _require_user_id(request), approval_id)
        except chat.ChatError as exc:
            raise _chat_error(exc)
        return _chat_found(row, "approval_not_found")

    # ── user settings ────────────────────────────────────────────────────────
    #
    # A free-form per-user JSON object (sections keyed at the top level, e.g.
    # "reviewer"). The path user_id is authoritative: like the rest of /v1 the
    # gate is the X-Internal-Secret middleware plus the Go backend, which only
    # ever passes the authenticated caller's own id.

    @app.get("/v1/user-settings/{user_id}")
    async def get_user_settings_handler(user_id: str, session: SessionDep) -> Any:
        # No row yet is not an error -- the user simply has no settings.
        return await user_settings.get_user_settings(session, user_id)

    @app.put("/v1/user-settings/{user_id}")
    async def put_user_settings_handler(
        user_id: str, session: SessionDep, body: Any = Body(default=None)
    ) -> Any:
        try:
            return await user_settings.put_user_settings(session, user_id, body)
        except user_settings.UserSettingsError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.message)

    @app.patch("/v1/user-settings/{user_id}")
    async def patch_user_settings_handler(
        user_id: str, session: SessionDep, body: Any = Body(default=None)
    ) -> Any:
        try:
            return await user_settings.patch_user_settings(session, user_id, body)
        except user_settings.UserSettingsError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.message)

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

    # ── dataset templates ────────────────────────────────────────────────────────

    from .stores import dataset_templates

    @app.get("/v1/dataset-templates", response_model=list[DatasetTemplateOut])
    async def list_dataset_templates_handler(request: Request, session: SessionDep) -> Any:
        user_id = _get_user_id(request)
        return await dataset_templates.list_templates(session, user_id=user_id, include_system=True)

    @app.get("/v1/dataset-templates/{id}", response_model=DatasetTemplateOut)
    async def get_dataset_template_handler(id: str, session: SessionDep) -> Any:
        template = await dataset_templates.get_template(session, id)
        if template is None:
            raise HTTPException(status_code=404, detail="not_found")
        return template

    @app.post("/v1/dataset-templates", response_model=DatasetTemplateOut, status_code=201)
    async def create_dataset_template_handler(
        body: DatasetTemplateCreate,
        request: Request,
        session: SessionDep,
    ) -> Any:
        user_id = _require_user_id(request)
        return await dataset_templates.create_template(session, body, user_id=user_id)

    @app.patch("/v1/dataset-templates/{id}", response_model=DatasetTemplateOut)
    async def update_dataset_template_handler(
        id: str,
        body: DatasetTemplateUpdate,
        request: Request,
        session: SessionDep,
    ) -> Any:
        user_id = _require_user_id(request)
        template = await dataset_templates.update_template(session, id, body, user_id=user_id)
        if template is None:
            raise HTTPException(status_code=404, detail="not_found_or_unauthorized")
        return template

    @app.delete("/v1/dataset-templates/{id}", status_code=204)
    async def delete_dataset_template_handler(
        id: str,
        request: Request,
        session: SessionDep,
    ) -> None:
        user_id = _require_user_id(request)
        deleted = await dataset_templates.delete_template(session, id, user_id=user_id)
        if deleted is None:
            raise HTTPException(status_code=404, detail="not_found_or_unauthorized")

    # ── error handler ────────────────────────────────────────────────────────

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request, exc: Exception):
        logger.exception("unhandled error: %s", exc)
        return JSONResponse(status_code=500, content={"error": "internal_server_error"})

    return app
