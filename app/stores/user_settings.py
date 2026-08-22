from __future__ import annotations

"""Per-user settings blob (one ``user_settings`` row per user).

The stored value is always a plain JSON object. ``get`` never 404s -- a user
with no row yet simply has no settings, which reads as ``{}``. ``put``
replaces the whole object, ``patch`` merges top-level keys (a key present in
the patch overwrites the stored key wholesale; nested merging is deliberately
NOT done so a caller can clear a section by writing a smaller one).
"""

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from ..models import User, UserSetting


class UserSettingsError(Exception):
    """Domain error carrying an HTTP status; routes translate it."""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def _require_object(value: object, field: str) -> dict:
    if not isinstance(value, dict):
        raise UserSettingsError(400, f"{field} must be a JSON object")
    return value


def _as_object(value: object) -> dict:
    """Defensive read: a legacy/NULL value must still surface as an object."""
    return dict(value) if isinstance(value, dict) else {}


async def _require_user(session: AsyncSession, user_id: str) -> None:
    if await session.get(User, user_id) is None:
        raise UserSettingsError(404, "user_not_found")


async def get_user_settings(session: AsyncSession, user_id: str) -> dict:
    row = await session.get(UserSetting, user_id)
    if row is None:
        return {}
    return _as_object(row.settings)


async def put_user_settings(session: AsyncSession, user_id: str, settings: dict) -> dict:
    """Replace the whole settings object (upsert). Returns what was stored."""
    _require_object(settings, "settings")
    await _require_user(session, user_id)

    row = await session.get(UserSetting, user_id)
    if row is None:
        row = UserSetting(user_id=user_id, settings=dict(settings))
        session.add(row)
    else:
        # A NEW dict (not an in-place mutation) is what makes SQLAlchemy see
        # the JSONB attribute as dirty; flag_modified belts-and-braces it.
        row.settings = dict(settings)
        flag_modified(row, "settings")
    await session.commit()
    await session.refresh(row)
    return _as_object(row.settings)


async def patch_user_settings(session: AsyncSession, user_id: str, patch: dict) -> dict:
    """Merge top-level keys into the stored object (upsert). Returns the merge."""
    _require_object(patch, "patch")
    await _require_user(session, user_id)

    row = await session.get(UserSetting, user_id)
    if row is None:
        row = UserSetting(user_id=user_id, settings=dict(patch))
        session.add(row)
    else:
        merged = _as_object(row.settings)
        merged.update(patch)
        row.settings = merged
        flag_modified(row, "settings")
    await session.commit()
    await session.refresh(row)
    return _as_object(row.settings)
