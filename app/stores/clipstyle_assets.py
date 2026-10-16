"""Frozen brand assets of approved clipstyle packages (clipstyle render Phase 4, migration 0039)."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ClipstyleAsset, MediaItem, RenderTemplate
from app.schemas import ClipstyleAssetOut
from app.stores.media import CLIPSTYLE_ASSET_KIND


async def check_assets_owned(session: AsyncSession, config: dict, user_id: str, *, lock: bool = False) -> None:
    """Every `config.assets` entry's media must exist, belong to `user_id`, and be a hidden asset
    (CLIPSTYLE_ASSET_KIND); raises `package_asset_missing` otherwise.

    Reused by `freeze_assets` (approval, ``lock=True`` — see there for why) and by
    `clipstyle_packages.check_package_assets_owned` (package create/update, a plain read — the
    spec's ⚠: a draft can never name another user's, or a non-asset, media in `config.assets`
    before it is ever approved)."""
    get_kwargs = {"with_for_update": {"read": True}} if lock else {}
    for _label, entry in sorted((config.get("assets") or {}).items()):
        media = await session.get(MediaItem, entry["media_id"], **get_kwargs)
        if media is None or media.user_id != user_id or (media.metadata_ or {}).get("kind") != CLIPSTYLE_ASSET_KIND:
            raise ValueError("package_asset_missing")


async def freeze_assets(session: AsyncSession, row: RenderTemplate) -> None:
    """Inserts one row per config.assets entry; part of the caller's (approval's) transaction.

    The ownership/kind check (`check_assets_owned`) reads each media row FOR SHARE (I2): this
    serialises against a concurrent `delete_media`, which takes FOR UPDATE on the same row before
    its frozen check. Either this freeze blocks until that delete's outcome is decided (and then
    sees the media gone, raising `package_asset_missing` instead of inserting a dangling asset), or
    the delete blocks until this transaction commits (and then sees the now-frozen row and refuses).
    """
    config = row.config or {}
    await check_assets_owned(session, config, row.user_id, lock=True)
    for label, entry in sorted((config.get("assets") or {}).items()):
        session.add(ClipstyleAsset(template_id=row.id, label=label, sha256=entry["sha256"], media_id=entry["media_id"], mime=entry["mime"]))


async def list_assets(session: AsyncSession, template_id: str) -> list[ClipstyleAssetOut]:
    stmt = select(ClipstyleAsset).where(ClipstyleAsset.template_id == template_id).order_by(ClipstyleAsset.label)
    return [ClipstyleAssetOut.model_validate(a) for a in (await session.execute(stmt)).scalars()]


async def media_is_frozen(session: AsyncSession, media_id: str) -> bool:
    stmt = select(ClipstyleAsset.id).where(ClipstyleAsset.media_id == media_id).limit(1)
    return (await session.execute(stmt)).first() is not None
