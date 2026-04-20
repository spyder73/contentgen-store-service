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


async def list_users(session: AsyncSession) -> list[User]:
    result = await session.execute(
        select(User).where(User.is_active.is_(True)).order_by(User.username)
    )
    return list(result.scalars())
