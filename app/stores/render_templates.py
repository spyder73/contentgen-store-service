from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import RenderProposal, RenderTemplate
from ..schemas import (
    RenderProposalIn,
    RenderProposalOut,
    RenderTemplateCloneIn,
    RenderTemplateIn,
    RenderTemplateOut,
)

VALID_TEMPLATE_SOURCES = {"builtin", "user_saved", "agent_generated"}
VALID_TEMPLATE_STATUSES = {"draft", "active", "archived"}
VALID_PROPOSAL_STATUSES = {"draft", "validated", "approved", "rejected", "failed"}


def _new_id() -> str:
    return str(uuid.uuid4())


def _require_valid(value: str, allowed: set[str], field: str) -> str:
    if value not in allowed:
        raise ValueError(f"invalid_{field}")
    return value


def _template_out(row: RenderTemplate) -> RenderTemplateOut:
    return RenderTemplateOut.model_validate(row)


def _proposal_out(row: RenderProposal) -> RenderProposalOut:
    return RenderProposalOut.model_validate(row)


async def list_templates(
    session: AsyncSession,
    *,
    user_id: str,
    kind: str | None = None,
    include_archived: bool = False,
) -> list[RenderTemplateOut]:
    stmt = select(RenderTemplate).where(
        or_(RenderTemplate.user_id == user_id, RenderTemplate.user_id.is_(None))
    )
    if kind:
        stmt = stmt.where(RenderTemplate.kind == kind)
    if not include_archived:
        stmt = stmt.where(RenderTemplate.status != "archived")
    stmt = stmt.order_by(RenderTemplate.created_at.desc(), RenderTemplate.name)
    result = await session.execute(stmt)
    return [_template_out(row) for row in result.scalars()]


async def get_template(
    session: AsyncSession, id: str, *, user_id: str
) -> RenderTemplateOut | None:
    row = await session.get(RenderTemplate, id)
    if row is None:
        return None
    if row.user_id is not None and row.user_id != user_id:
        return None
    return _template_out(row)


async def create_template(
    session: AsyncSession, body: RenderTemplateIn, *, user_id: str
) -> RenderTemplateOut:
    row = RenderTemplate(id=body.id or _new_id())
    _apply_template_body(row, body)
    row.user_id = user_id
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return _template_out(row)


async def update_template(
    session: AsyncSession, id: str, body: RenderTemplateIn, *, user_id: str
) -> RenderTemplateOut | None:
    row = await session.get(RenderTemplate, id)
    if row is None or row.user_id != user_id:
        return None
    _apply_template_body(row, body)
    row.user_id = user_id
    await session.commit()
    await session.refresh(row)
    return _template_out(row)


async def archive_template(session: AsyncSession, id: str, *, user_id: str) -> bool:
    row = await session.get(RenderTemplate, id)
    if row is None or row.user_id != user_id:
        return False
    row.status = "archived"
    await session.commit()
    return True


async def clone_template(
    session: AsyncSession, id: str, body: RenderTemplateCloneIn, *, user_id: str
) -> RenderTemplateOut | None:
    source = await session.get(RenderTemplate, id)
    if source is None:
        return None
    if source.user_id is not None and source.user_id != user_id:
        return None
    row = RenderTemplate(
        id=body.id or _new_id(),
        user_id=user_id,
        name=body.name or f"{source.name} Copy",
        description=source.description or "",
        kind=source.kind or "carousel",
        source="user_saved",
        status="active",
        config=dict(source.config or {}),
        preview_url=source.preview_url,
        created_from_clip_id=source.created_from_clip_id,
        created_from_instruction=source.created_from_instruction,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return _template_out(row)


def _apply_template_body(row: RenderTemplate, body: RenderTemplateIn) -> None:
    row.name = body.name
    row.description = body.description
    row.kind = body.kind or "carousel"
    row.source = _require_valid(body.source or "user_saved", VALID_TEMPLATE_SOURCES, "source")
    row.status = _require_valid(body.status or "active", VALID_TEMPLATE_STATUSES, "status")
    row.config = body.config or {}
    row.preview_url = body.preview_url
    row.created_from_clip_id = body.created_from_clip_id
    row.created_from_instruction = body.created_from_instruction


async def create_proposal(
    session: AsyncSession, body: RenderProposalIn, *, user_id: str
) -> RenderProposalOut:
    status = _require_valid(body.status or "draft", VALID_PROPOSAL_STATUSES, "status")
    if body.source_template_id:
        template = await session.get(RenderTemplate, body.source_template_id)
        if template is None or (template.user_id is not None and template.user_id != user_id):
            raise ValueError("source_template_not_found")
    row = RenderProposal(
        id=body.id or _new_id(),
        user_id=user_id,
        clip_id=body.clip_id,
        kind=body.kind or "carousel_design",
        status=status,
        instruction=body.instruction or "",
        source_template_id=body.source_template_id,
        metadata_patch_json=body.metadata_patch_json or {},
        template_config_json=body.template_config_json or {},
        preview_output_refs_json=body.preview_output_refs_json or [],
        validation_report_json=body.validation_report_json or {},
        approved_at=datetime.now(timezone.utc) if status == "approved" else None,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return _proposal_out(row)


async def list_proposals(
    session: AsyncSession, *, user_id: str, clip_id: str | None = None
) -> list[RenderProposalOut]:
    stmt = select(RenderProposal).where(RenderProposal.user_id == user_id)
    if clip_id:
        stmt = stmt.where(RenderProposal.clip_id == clip_id)
    stmt = stmt.order_by(RenderProposal.created_at.desc())
    result = await session.execute(stmt)
    return [_proposal_out(row) for row in result.scalars()]


async def get_proposal(
    session: AsyncSession, id: str, *, user_id: str
) -> RenderProposalOut | None:
    row = await session.get(RenderProposal, id)
    if row is None or row.user_id != user_id:
        return None
    return _proposal_out(row)


async def set_proposal_status(
    session: AsyncSession,
    id: str,
    status: str,
    *,
    user_id: str,
    validation_report_json: dict | None = None,
) -> RenderProposalOut | None:
    row = await session.get(RenderProposal, id)
    if row is None or row.user_id != user_id:
        return None
    row.status = _require_valid(status, VALID_PROPOSAL_STATUSES, "status")
    if validation_report_json is not None:
        row.validation_report_json = validation_report_json
    row.approved_at = datetime.now(timezone.utc) if row.status == "approved" else None
    await session.commit()
    await session.refresh(row)
    return _proposal_out(row)
