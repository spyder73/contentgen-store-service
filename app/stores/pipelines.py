from __future__ import annotations

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import PipelineTemplate, PipelineTemplateAssignment, User
from ..schemas import PipelineTemplateIn, PipelineTemplateOut


VALID_VISIBILITY = {"private", "assigned", "global"}


async def _assigned_user_ids(session: AsyncSession, template_id: str) -> list[str]:
    result = await session.execute(
        select(PipelineTemplateAssignment.user_id)
        .where(PipelineTemplateAssignment.template_id == template_id)
        .order_by(PipelineTemplateAssignment.user_id)
    )
    return list(result.scalars())


async def _out(session: AsyncSession, row: PipelineTemplate) -> PipelineTemplateOut:
    out = PipelineTemplateOut.model_validate(row)
    out.assigned_user_ids = await _assigned_user_ids(session, row.id)
    return out


async def is_admin(session: AsyncSession, user_id: str | None) -> bool:
    if not user_id:
        return False
    result = await session.execute(select(User.is_admin).where(User.id == user_id))
    return bool(result.scalar_one_or_none())


async def list_pipelines(session: AsyncSession, user_id: str | None = None, admin: bool = False) -> list[PipelineTemplateOut]:
    stmt = select(PipelineTemplate)
    if not admin and user_id:
        assigned = (
            select(PipelineTemplateAssignment.template_id)
            .where(PipelineTemplateAssignment.user_id == user_id)
        )
        stmt = stmt.where(
            or_(
                PipelineTemplate.user_id == user_id,
                PipelineTemplate.visibility == "global",
                PipelineTemplate.id.in_(assigned),
            )
        )
    elif not admin and user_id is not None:
        stmt = stmt.where(PipelineTemplate.visibility == "global")
    stmt = stmt.order_by(PipelineTemplate.name)
    result = await session.execute(stmt)
    return [await _out(session, row) for row in result.scalars()]


async def get_pipeline(session: AsyncSession, id: str, user_id: str | None = None) -> PipelineTemplateOut | None:
    row = await session.get(PipelineTemplate, id)
    if row is None:
        return None
    if await is_admin(session, user_id):
        return await _out(session, row)
    if row.user_id == user_id or row.visibility == "global":
        return await _out(session, row)
    if user_id:
        assigned = await session.get(PipelineTemplateAssignment, {"template_id": id, "user_id": user_id})
        if assigned is not None:
            return await _out(session, row)
    return None


async def upsert_pipeline(
    session: AsyncSession,
    body: PipelineTemplateIn,
    user_id: str | None = None,
    admin: bool = False,
) -> PipelineTemplateOut:
    visibility = body.visibility if body.visibility in VALID_VISIBILITY else "private"
    row = await session.get(PipelineTemplate, body.id)
    if row is None:
        row = PipelineTemplate(id=body.id)
        session.add(row)
    else:
        if not admin and row.user_id is not None and (user_id is None or row.user_id != user_id):
            return await _out(session, row)
        if not admin and row.user_id is None and row.visibility != "global":
            return await _out(session, row)
    row.name = body.name
    row.data = body.data
    row.version = body.version
    row.visibility = visibility if admin else "private"
    if admin:
        row.user_id = body.user_id
    elif row.user_id is None and user_id is not None:
        row.user_id = user_id
    await session.flush()
    if admin:
        await set_pipeline_assignments(session, body.id, body.assigned_user_ids, commit=False)
    await session.commit()
    await session.refresh(row)
    return await _out(session, row)


async def delete_pipeline(session: AsyncSession, id: str, user_id: str | None = None, admin: bool = False) -> bool:
    row = await session.get(PipelineTemplate, id)
    if row is None:
        return False
    if not admin and row.user_id != user_id:
        return False
    result = await session.execute(delete(PipelineTemplate).where(PipelineTemplate.id == id))
    await session.commit()
    return result.rowcount > 0


async def set_pipeline_visibility(session: AsyncSession, id: str, visibility: str) -> PipelineTemplateOut | None:
    if visibility not in VALID_VISIBILITY:
        visibility = "private"
    row = await session.get(PipelineTemplate, id)
    if row is None:
        return None
    row.visibility = visibility
    await session.commit()
    await session.refresh(row)
    return await _out(session, row)


async def set_pipeline_assignments(
    session: AsyncSession,
    id: str,
    user_ids: list[str],
    *,
    commit: bool = True,
) -> PipelineTemplateOut | None:
    row = await session.get(PipelineTemplate, id)
    if row is None:
        return None
    normalized = sorted({u.strip() for u in user_ids if u and u.strip()})
    await session.execute(delete(PipelineTemplateAssignment).where(PipelineTemplateAssignment.template_id == id))
    for uid in normalized:
        session.add(PipelineTemplateAssignment(template_id=id, user_id=uid))
    if commit:
        await session.commit()
        await session.refresh(row)
    return await _out(session, row)
