"""Frozen render bundles of approved clipstyle packages (Phase 4).

A render bundle is the webpack bundle the validator built for one package version. Remotion's bundle
and renderer versions must match, so a bundle is keyed by the runtime image it was built with
(`image_ref`). The bridge uploads the bundle as hidden media (kind `clipstyle_bundle`) at every
stored passing validation and freezes it here when the version is approved, or when its backfill
re-validates an approved version on a new image. Rows are append-only (migration 0038 trigger).
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import ClipstyleRenderBundle, RenderTemplate
from ..schemas import ClipstyleRenderBundleIn, ClipstyleRenderBundleOut
from .clipstyle_packages import PACKAGE_KIND


def _out(row: ClipstyleRenderBundle) -> ClipstyleRenderBundleOut:
    return ClipstyleRenderBundleOut.model_validate(row)


def _same(row: ClipstyleRenderBundle, body: ClipstyleRenderBundleIn) -> bool:
    return row.bundle_sha256 == body.bundle_sha256 and row.media_id == body.media_id


async def _for_image(session: AsyncSession, template_id: str, image_ref: str) -> ClipstyleRenderBundle | None:
    stmt = select(ClipstyleRenderBundle).where(
        ClipstyleRenderBundle.template_id == template_id, ClipstyleRenderBundle.image_ref == image_ref
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def add_bundle(session: AsyncSession, template_id: str, body: ClipstyleRenderBundleIn) -> ClipstyleRenderBundleOut | None:
    """Freezes `body` as the bundle of an approved package version on `body.image_ref`. None when
    `template_id` is not a package row; ValueError("package_not_approved") before approval;
    ValueError("bundle_frozen") when another bundle is already frozen for that image. Sending the
    frozen bundle again returns it (a bridge retry after a lost answer)."""
    row = await session.get(RenderTemplate, template_id)
    if row is None or row.kind != PACKAGE_KIND:
        return None
    if row.status != "approved":
        raise ValueError("package_not_approved")
    existing = await _for_image(session, template_id, body.image_ref)
    if existing is not None:
        if _same(existing, body):
            return _out(existing)
        raise ValueError("bundle_frozen")
    bundle = ClipstyleRenderBundle(
        template_id=template_id, bundle_sha256=body.bundle_sha256, media_id=body.media_id, image_ref=body.image_ref
    )
    session.add(bundle)
    try:
        await session.commit()
    except IntegrityError:  # a concurrent freeze for the same image may have won the unique index
        await session.rollback()
        existing = await _for_image(session, template_id, body.image_ref)
        if existing is None:
            # Not the (template_id, image_ref) uniqueness conflict after all (e.g. a missing FK
            # target) — surface the real error instead of misreporting it as bundle_frozen.
            raise
        if _same(existing, body):
            return _out(existing)
        raise ValueError("bundle_frozen") from None
    await session.refresh(bundle)
    return _out(bundle)


async def list_bundles(session: AsyncSession, template_id: str) -> list[ClipstyleRenderBundleOut]:
    stmt = (
        select(ClipstyleRenderBundle)
        .where(ClipstyleRenderBundle.template_id == template_id)
        .order_by(ClipstyleRenderBundle.created_at, ClipstyleRenderBundle.id)
    )
    return [_out(row) for row in (await session.execute(stmt)).scalars()]
