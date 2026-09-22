from __future__ import annotations

from sqlalchemy import BigInteger, cast, delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
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
    # A run is saved asynchronously before and after provider calls. HTTP
    # completion order is not generation order: an older dispatched snapshot
    # must never overwrite a newer completed ledger after a backend restart.
    # Keep the revision in JSONB so existing installations need no migration.
    raw_version = body.snapshot.get("_snapshot_version", 0)
    version = raw_version if isinstance(raw_version, int) and not isinstance(raw_version, bool) else 0
    version = max(0, min(version, 2**63 - 1))
    snapshot = dict(body.snapshot)
    snapshot["_snapshot_version"] = version
    dialect = session.get_bind().dialect.name
    insert = sqlite_insert if dialect == "sqlite" else pg_insert
    statement = insert(PipelineRunSnapshot).values(
        id=body.id, user_id=user_id, status=body.status, snapshot=snapshot,
    )
    existing_version = func.coalesce(
        cast(PipelineRunSnapshot.snapshot["_snapshot_version"].as_string(), BigInteger), 0
    )
    # An unversioned writer (a backend that predates revisions, or one rolled
    # back to such a build) always applies: only one backend runs at a time,
    # so its saves are the newest state and dropping them would silently stop
    # run persistence after a rollback. A versioned writer applies only over
    # an older revision; equal revisions are idempotent retries.
    changes = {"status": body.status, "snapshot": snapshot, "updated_at": func.now()}
    if version == 0:
        statement = statement.on_conflict_do_update(
            index_elements=[PipelineRunSnapshot.id], set_=changes,
        )
    else:
        statement = statement.on_conflict_do_update(
            index_elements=[PipelineRunSnapshot.id], set_=changes,
            where=existing_version < version,
        )
    await session.execute(statement)
    await session.commit()
    row = (await session.execute(
        select(PipelineRunSnapshot).where(PipelineRunSnapshot.id == body.id)
        .execution_options(populate_existing=True)
    )).scalar_one()
    return PipelineRunSnapshotOut.model_validate(row)


async def delete_snapshot(session: AsyncSession, id: str) -> bool:
    result = await session.execute(delete(PipelineRunSnapshot).where(PipelineRunSnapshot.id == id))
    await session.commit()
    return result.rowcount > 0


async def delete_all_snapshots(session: AsyncSession) -> int:
    result = await session.execute(delete(PipelineRunSnapshot))
    await session.commit()
    return result.rowcount
