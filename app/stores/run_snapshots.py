from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import PipelineRunSnapshot
from ..schemas import PipelineRunSnapshotIn, PipelineRunSnapshotOut


async def list_snapshots(
    session: AsyncSession, user_id: str | None = None
) -> list[PipelineRunSnapshotOut]:
    stmt = select(PipelineRunSnapshot)
    if user_id:
        stmt = stmt.where(PipelineRunSnapshot.user_id == user_id)
    stmt = stmt.order_by(PipelineRunSnapshot.created_at)
    result = await session.execute(stmt)
    return [PipelineRunSnapshotOut.model_validate(row) for row in result.scalars()]


async def get_snapshot(session: AsyncSession, id: str) -> PipelineRunSnapshotOut | None:
    row = await session.get(PipelineRunSnapshot, id)
    if row is None:
        return None
    return PipelineRunSnapshotOut.model_validate(row)


async def upsert_snapshot(
    session: AsyncSession, body: PipelineRunSnapshotIn, user_id: str | None = None
) -> PipelineRunSnapshotOut:
    # Trust model: reached only via the internal-secret-gated service-to-service
    # PUT route; the Go backend always writes the run's own user, so we stamp
    # user_id on insert and never enforce ownership on update (unlike
    # upsert_pipeline, which refuses cross-user overwrites for end-user callers).
    row = await session.get(PipelineRunSnapshot, body.id)
    if row is None:
        row = PipelineRunSnapshot(id=body.id)
        if user_id:
            row.user_id = user_id
        session.add(row)
    row.status = body.status
    row.snapshot = body.snapshot
    await session.commit()
    await session.refresh(row)
    return PipelineRunSnapshotOut.model_validate(row)
