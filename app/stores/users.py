from __future__ import annotations

import bcrypt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import User


async def verify_credentials(
    session: AsyncSession, username: str, password: str
) -> User | None:
    """Return the user if credentials are valid, else None."""
    result = await session.execute(
        select(User).where(User.username == username, User.is_active.is_(True))
    )
    user = result.scalar_one_or_none()
    if user is None:
        return None
    if not bcrypt.checkpw(password.encode(), user.password_hash.encode()):
        return None
    return user


async def get_user_by_id(session: AsyncSession, user_id: str) -> User | None:
    return await session.get(User, user_id)


async def get_user_by_username(session: AsyncSession, username: str) -> User | None:
    result = await session.execute(
        select(User).where(User.username == username, User.is_active.is_(True))
    )
    return result.scalar_one_or_none()


async def list_users(session: AsyncSession) -> list[User]:
    result = await session.execute(
        select(User).where(User.is_active.is_(True)).order_by(User.username)
    )
    return list(result.scalars())


class DailyLimitError(Exception):
    """Domain error carrying an HTTP status; the route translates it."""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def _require_daily_limit(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DailyLimitError(400, "daily_spend_limit must be a non-negative integer")
    if value < 0:
        raise DailyLimitError(400, "daily_spend_limit must be a non-negative integer")
    return value


async def set_daily_spend_limit(session: AsyncSession, user_id: str, body: object) -> dict:
    """Admin-only in practice (gated by the caller): set a user's daily credit
    spend cap used by ``credits.reserve``. Returns the stored value."""
    if not isinstance(body, dict):
        raise DailyLimitError(400, "body must be a JSON object")
    limit = _require_daily_limit(body.get("daily_spend_limit"))

    user = await session.get(User, user_id)
    if user is None:
        raise DailyLimitError(404, "user_not_found")

    user.daily_spend_limit = limit
    await session.commit()
    await session.refresh(user)
    return {"user_id": user.id, "daily_spend_limit": user.daily_spend_limit}
