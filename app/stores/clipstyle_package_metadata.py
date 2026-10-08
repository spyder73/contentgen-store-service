"""Metadata-only list of the caller's clipstyle package rows (#3 Task 19 carry 5).

A clipstyle package row's ``config`` carries the package files, up to 512 KiB of them. The Go
backend's owner-only ``GET /clipstyles`` filter needs only each row's id and owner, yet
``GET /v1/render-templates?kind=clipstyle_package`` answers every row whole. This query selects
id/owner/name/status and extracts the manifest's style id and version in SQL, so the files never
leave the database.
"""
from __future__ import annotations

import re

from sqlalchemy import JSON, Select, func, select, type_coerce
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import RenderTemplate
from ..schemas import ClipstylePackageMetadataOut, ClipstylePackageMetadataPage

PACKAGE_KIND = "clipstyle_package"
# Pages are bounded: a user's built packages are not capped, and the caller reads every page
# within its own time budget (fail-closed for anything it does not reach).
DEFAULT_LIMIT = 500
MAX_LIMIT = 1000

_INTEGER_TEXT = re.compile(r"-?\d+")


def _owned(user_id: str, include_archived: bool) -> list:
    where = [RenderTemplate.user_id == user_id, RenderTemplate.kind == PACKAGE_KIND]
    if not include_archived:
        where.append(RenderTemplate.status != "archived")
    return where


def metadata_query(*, user_id: str, include_archived: bool = False) -> Select:
    """The caller's own package rows (never system or other users' rows), newest first. The id
    breaks ties, so the order is total: without concurrent writes, consecutive pages neither
    overlap nor skip a row (a row archived between two pages can shift one past the reader)."""
    # Generic JSON paths: ``#>>`` on Postgres, JSON_EXTRACT on sqlite (the test database).
    config = type_coerce(RenderTemplate.config, JSON)
    stmt = select(
        RenderTemplate.id,
        RenderTemplate.user_id,
        RenderTemplate.name,
        RenderTemplate.status,
        config[("manifest", "id")].as_string().label("style_id"),
        config[("manifest", "version")].as_string().label("version"),
        RenderTemplate.created_at,
        RenderTemplate.updated_at,
    ).where(*_owned(user_id, include_archived))
    return stmt.order_by(RenderTemplate.created_at.desc(), RenderTemplate.name, RenderTemplate.id)


async def list_package_metadata(
    session: AsyncSession,
    *,
    user_id: str,
    include_archived: bool = False,
    page: int = 1,
    limit: int = DEFAULT_LIMIT,
) -> ClipstylePackageMetadataPage:
    total = (
        await session.execute(select(func.count()).select_from(RenderTemplate).where(*_owned(user_id, include_archived)))
    ).scalar_one()
    stmt = metadata_query(user_id=user_id, include_archived=include_archived).offset((page - 1) * limit).limit(limit)
    result = await session.execute(stmt)
    items = [
        ClipstylePackageMetadataOut(
            id=row.id,
            user_id=None if row.user_id is None else str(row.user_id),
            name=row.name,
            status=row.status,
            style_id=None if row.style_id is None else str(row.style_id),
            version=_version(row.version),
            created_at=row.created_at,
            updated_at=row.updated_at,
        )
        for row in result
    ]
    return ClipstylePackageMetadataPage(items=items, total=total, page=page, limit=limit)


def _version(value: object) -> int | None:
    """A manifest version as stored: an integer (Postgres hands it back as text), else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and _INTEGER_TEXT.fullmatch(value.strip()):
        return int(value.strip())
    return None
