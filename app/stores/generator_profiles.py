from __future__ import annotations

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import GeneratorProfile
from ..schemas import (
    GeneratorProfileCreate,
    GeneratorProfileOut,
    GeneratorProfileUpdate,
)

REQUIRED_SPEC_KEYS = ("base_model", "adapters", "prompt", "params")


class GeneratorProfileError(Exception):
    """Domain error carrying an HTTP status and a clear message.

    Routes translate this into an HTTPException so the registry / Go backend get
    actionable 400/409 responses instead of opaque 500s.
    """

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def _validate_spec(spec: dict) -> dict:
    """Loose validation: required keys present and ``adapters`` is a list.

    We intentionally do not validate the inner shape of base_model/prompt/params
    so the spec can evolve without store-service changes.
    """
    if not isinstance(spec, dict):
        raise GeneratorProfileError(400, "spec must be an object")
    missing = [k for k in REQUIRED_SPEC_KEYS if k not in spec]
    if missing:
        raise GeneratorProfileError(
            400, f"spec missing required keys: {', '.join(missing)}"
        )
    if not isinstance(spec["adapters"], list):
        raise GeneratorProfileError(400, "spec.adapters must be a list")
    return spec


def _out(row: GeneratorProfile) -> GeneratorProfileOut:
    return GeneratorProfileOut.model_validate(row)


async def list_profiles(
    session: AsyncSession,
    *,
    status: str | None = None,
    user_id: str | None = None,
    slug: str | None = None,
) -> list[GeneratorProfileOut]:
    stmt = select(GeneratorProfile)
    if status:
        stmt = stmt.where(GeneratorProfile.status == status)
    if user_id:
        stmt = stmt.where(GeneratorProfile.user_id == user_id)
    if slug:
        stmt = stmt.where(GeneratorProfile.slug == slug)
    # Newest version first within a slug; group slugs together deterministically.
    stmt = stmt.order_by(GeneratorProfile.slug, GeneratorProfile.version.desc())
    result = await session.execute(stmt)
    return [_out(row) for row in result.scalars()]


async def get_profile(session: AsyncSession, id: str) -> GeneratorProfileOut | None:
    row = await session.get(GeneratorProfile, id)
    if row is None:
        return None
    return _out(row)


def parse_ref(ref: str) -> tuple[str, int]:
    """Split a "slug@version" ref. Raises GeneratorProfileError(400) if malformed."""
    slug, sep, version_str = ref.rpartition("@")
    if not sep or not slug or not version_str:
        raise GeneratorProfileError(400, "ref must be of the form 'slug@version'")
    try:
        version = int(version_str)
    except ValueError:
        raise GeneratorProfileError(400, "ref version must be an integer")
    return slug, version


async def get_profile_by_ref(
    session: AsyncSession, ref: str
) -> GeneratorProfileOut | None:
    slug, version = parse_ref(ref)
    stmt = select(GeneratorProfile).where(
        GeneratorProfile.slug == slug, GeneratorProfile.version == version
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        return None
    return _out(row)


async def create_profile(
    session: AsyncSession, body: GeneratorProfileCreate
) -> GeneratorProfileOut:
    spec = _validate_spec(body.spec)
    # A slug holds at most one draft at a time.
    existing_draft = (
        await session.execute(
            select(GeneratorProfile.id).where(
                GeneratorProfile.slug == body.slug,
                GeneratorProfile.status == "draft",
            )
        )
    ).first()
    if existing_draft is not None:
        raise GeneratorProfileError(
            409, f"a draft already exists for slug '{body.slug}'"
        )
    max_version = (
        await session.execute(
            select(func.max(GeneratorProfile.version)).where(
                GeneratorProfile.slug == body.slug
            )
        )
    ).scalar_one()
    next_version = (max_version or 0) + 1
    row = GeneratorProfile(
        slug=body.slug,
        version=next_version,
        status="draft",
        user_id=body.user_id,
        name=body.name,
        media_type=body.media_type or "image",
        spec=spec,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return _out(row)


async def update_profile(
    session: AsyncSession, id: str, body: GeneratorProfileUpdate
) -> GeneratorProfileOut | None:
    row = await session.get(GeneratorProfile, id)
    if row is None:
        return None
    if row.status == "published":
        raise GeneratorProfileError(
            409, "published profiles are immutable; create a new draft version"
        )
    if body.name is not None:
        row.name = body.name
    if body.spec is not None:
        row.spec = _validate_spec(body.spec)
    await session.commit()
    await session.refresh(row)
    return _out(row)


async def publish_profile(
    session: AsyncSession, id: str
) -> GeneratorProfileOut | None:
    row = await session.get(GeneratorProfile, id)
    if row is None:
        return None
    if row.status == "published":
        raise GeneratorProfileError(409, "profile is already published")
    row.status = "published"
    await session.commit()
    await session.refresh(row)
    return _out(row)


async def delete_profile(session: AsyncSession, id: str) -> bool | None:
    """Delete a draft. Returns None if absent, raises 409 for published rows."""
    row = await session.get(GeneratorProfile, id)
    if row is None:
        return None
    if row.status == "published":
        raise GeneratorProfileError(
            409, "published profiles cannot be deleted (may be referenced)"
        )
    await session.execute(
        delete(GeneratorProfile).where(GeneratorProfile.id == id)
    )
    await session.commit()
    return True
