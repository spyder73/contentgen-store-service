"""Frozen render bundles of approved clipstyle packages (Phase 4).

A render bundle is the webpack bundle the validator built for one package version. Remotion's bundle
and renderer versions must match, so a bundle is keyed by the runtime image it was built with
(`image_ref`). The bridge uploads the bundle as hidden media (kind `clipstyle_bundle`) at every
stored passing validation and freezes it here when the version is approved, or when its backfill
re-validates an approved version on a new image. Rows are append-only (migration 0038 trigger).
"""
from __future__ import annotations

from sqlalchemy import JSON, select, type_coerce
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import ClipstyleRenderBundle, RenderTemplate
from ..schemas import (
    ClipstyleCatalogOut,
    ClipstylePackageResolveOut,
    ClipstyleRenderBundleIn,
    ClipstyleRenderBundleOut,
    ClipstyleUsableOut,
)
from .clipstyle_packages import PACKAGE_KIND

OWNER_VISIBILITY = ("private", "published")
ADMIN_VISIBILITY = ("private", "revoked")
MAX_USABLE = 500
MAX_CATALOG = 1000


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


def _manifest_version(value: object) -> int:
    """A manifest version as selected (Postgres hands JSON back as text): an integer, else 0."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return 0


async def resolve_package(
    session: AsyncSession, *, style: str, version: int | None, caller: str
) -> ClipstylePackageResolveOut | None:
    """The approved package row of `style` (pinned `version`, else the highest approved one) and its
    frozen bundles; None when no approved package row has that style (and version). Selects only
    the columns it answers with, as clipstyle_package_metadata.py does: a row's config carries up
    to 512 KiB of files, and this runs on every render and fit check (M-22)."""
    config = type_coerce(RenderTemplate.config, JSON)
    stmt = select(
        RenderTemplate.id,
        RenderTemplate.user_id,
        RenderTemplate.visibility,
        config[("manifest", "version")].as_string().label("version"),
        config["implements"].as_string().label("implements"),
    ).where(
        RenderTemplate.kind == PACKAGE_KIND,
        RenderTemplate.status == "approved",
        config[("manifest", "id")].as_string() == style,
    )
    rows = [r for r in (await session.execute(stmt)) if version is None or _manifest_version(r.version) == version]
    if not rows:
        return None
    row = max(rows, key=lambda r: _manifest_version(r.version))
    bundles = await list_bundles(session, row.id)
    implements = row.implements
    owner = None if row.user_id is None else str(row.user_id)
    return ClipstylePackageResolveOut(
        template_id=row.id,
        owner=owner,
        caller_is_owner=owner is not None and owner == caller,
        visibility=row.visibility,
        style=style,
        version=_manifest_version(row.version),
        has_bundle=bool(bundles),
        image_refs=[b.image_ref for b in bundles],
        implements=implements if isinstance(implements, str) else None,
    )


async def set_visibility(
    session: AsyncSession, id: str, visibility: str, *, owner: str | None = None, admin: bool = False
) -> dict | None:
    """The only writer of render_templates.visibility (design § Visibility and publishing): the
    owner toggles private <-> published of their approved version (never a revoked one); an admin
    sets private (unpublish, or lift a revoke) or revoked. Content is never touched."""
    allowed = ADMIN_VISIBILITY if admin else OWNER_VISIBILITY
    if visibility not in allowed:
        raise ValueError("invalid_visibility")
    row = await session.get(RenderTemplate, id)
    if row is None or row.kind != PACKAGE_KIND or (not admin and (row.user_id is None or str(row.user_id) != owner)):
        return None
    if row.status != "approved":
        raise ValueError("package_not_approved")
    if not admin and row.visibility == "revoked":
        raise ValueError("package_revoked")
    row.visibility = visibility
    await session.commit()
    return {"id": row.id, "visibility": row.visibility}


async def list_usable(session: AsyncSession, *, caller: str) -> list[ClipstyleUsableOut]:
    """Approved package rows the caller may use: their own except revoked, plus everyone's
    published ones (design § Visibility and publishing). Selects only the columns and JSON paths
    it answers with, as resolve_package does (M-22): no package files, ever."""
    config = type_coerce(RenderTemplate.config, JSON)
    stmt = (
        select(
            RenderTemplate.id,
            RenderTemplate.user_id,
            RenderTemplate.visibility,
            config[("manifest", "id")].as_string().label("style"),
            config[("manifest", "version")].as_string().label("version"),
            config["implements"].as_string().label("implements"),
        )
        .where(
            RenderTemplate.kind == PACKAGE_KIND,
            RenderTemplate.status == "approved",
            ((RenderTemplate.user_id == caller) & (RenderTemplate.visibility != "revoked"))
            | (RenderTemplate.visibility == "published"),
        )
        .order_by(RenderTemplate.approved_at.desc(), RenderTemplate.id)
        .limit(MAX_USABLE)
    )
    out = []
    for row in await session.execute(stmt):
        if row.style is None or row.version is None:
            continue
        owner = None if row.user_id is None else str(row.user_id)
        out.append(
            ClipstyleUsableOut(
                template_id=row.id,
                owner=owner,
                caller_is_owner=owner == caller,
                style=row.style,
                version=_manifest_version(row.version),
                visibility=row.visibility,
                implements=row.implements,
            )
        )
    return out


async def list_catalog(session: AsyncSession) -> list[ClipstyleCatalogOut]:
    """Every approved package row, for the admin catalog (design § Visibility and publishing).
    Selects only the columns and JSON paths it answers with, as resolve_package does (M-22): no
    package files, ever."""
    config = type_coerce(RenderTemplate.config, JSON)
    stmt = (
        select(
            RenderTemplate.id,
            RenderTemplate.user_id,
            RenderTemplate.name,
            RenderTemplate.visibility,
            RenderTemplate.approved_at,
            config[("manifest", "id")].as_string().label("style"),
            config[("manifest", "version")].as_string().label("version"),
        )
        .where(RenderTemplate.kind == PACKAGE_KIND, RenderTemplate.status == "approved")
        .order_by(RenderTemplate.approved_at.desc(), RenderTemplate.id)
        .limit(MAX_CATALOG)
    )
    out = []
    for row in await session.execute(stmt):
        out.append(
            ClipstyleCatalogOut(
                template_id=row.id,
                owner=None if row.user_id is None else str(row.user_id),
                name=row.name,
                style=row.style,
                version=_manifest_version(row.version) if row.version is not None else None,
                visibility=row.visibility,
                approved_at=row.approved_at,
            )
        )
    return out
