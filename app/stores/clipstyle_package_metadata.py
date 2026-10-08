"""Metadata-only list of the caller's clipstyle package rows (#3 Task 19 carry 5).

A clipstyle package row's ``config`` carries the package files, up to 512 KiB of them. The Go
backend's owner-only ``GET /clipstyles`` filter needs only each row's id and owner, yet
``GET /v1/render-templates?kind=clipstyle_package`` answers every row whole. This query selects
id/owner/name/status and extracts the manifest's style id and version in SQL, so the files never
leave the database.
"""
from __future__ import annotations

import re

from sqlalchemy import JSON, Select, select, type_coerce
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import RenderTemplate
from ..schemas import ClipstylePackageMetadataOut

PACKAGE_KIND = "clipstyle_package"

_INTEGER_TEXT = re.compile(r"-?\d+")


def metadata_query(*, user_id: str, include_archived: bool = False) -> Select:
    """The caller's own package rows (never system or other users' rows), newest first."""
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
    ).where(RenderTemplate.user_id == user_id, RenderTemplate.kind == PACKAGE_KIND)
    if not include_archived:
        stmt = stmt.where(RenderTemplate.status != "archived")
    return stmt.order_by(RenderTemplate.created_at.desc(), RenderTemplate.name)


async def list_package_metadata(
    session: AsyncSession, *, user_id: str, include_archived: bool = False
) -> list[ClipstylePackageMetadataOut]:
    result = await session.execute(metadata_query(user_id=user_id, include_archived=include_archived))
    return [
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


def _version(value: object) -> int | None:
    """A manifest version as stored: an integer (Postgres hands it back as text), else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and _INTEGER_TEXT.fullmatch(value.strip()):
        return int(value.strip())
    return None
