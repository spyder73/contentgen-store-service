"""Frozen brand assets of approved clipstyle packages (clipstyle render Phase 4, migration 0039)."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ClipstyleAsset, MediaItem, RenderTemplate
from app.schemas import ClipstyleAssetOut
from app.stores.media import CLIPSTYLE_ASSET_KIND


async def freeze_assets(session: AsyncSession, row: RenderTemplate) -> None:
    """Inserts one row per config.assets entry; part of the caller's (approval's) transaction.
    Each media must exist, belong to the package owner and be a hidden asset."""
    for label, entry in sorted(((row.config or {}).get("assets") or {}).items()):
        media = await session.get(MediaItem, entry["media_id"])
        if media is None or media.user_id != row.user_id or (media.metadata_ or {}).get("kind") != CLIPSTYLE_ASSET_KIND:
            raise ValueError("package_asset_missing")
        session.add(ClipstyleAsset(template_id=row.id, label=label, sha256=entry["sha256"], media_id=entry["media_id"], mime=entry["mime"]))


async def list_assets(session: AsyncSession, template_id: str) -> list[ClipstyleAssetOut]:
    stmt = select(ClipstyleAsset).where(ClipstyleAsset.template_id == template_id).order_by(ClipstyleAsset.label)
    return [ClipstyleAssetOut.model_validate(a) for a in (await session.execute(stmt)).scalars()]


async def media_is_frozen(session: AsyncSession, media_id: str) -> bool:
    stmt = select(ClipstyleAsset.id).where(ClipstyleAsset.media_id == media_id).limit(1)
    return (await session.execute(stmt)).first() is not None
