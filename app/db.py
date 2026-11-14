from __future__ import annotations

import os
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

DATABASE_URL: str = os.environ.get("DATABASE_URL", "")

_engine = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


class Base(DeclarativeBase):
    pass


def make_engine(url: str, **kwargs) -> AsyncEngine:
    """Every engine of the store is made here. ``hide_parameters`` keeps the bound values of a
    statement (prompts, file_url, metadata, usernames, password hashes) out of the text of a
    SQLAlchemy error, so out of every log line and the /healthz migration status that carries one."""
    return create_async_engine(url, hide_parameters=True, **kwargs)


def get_engine():
    global _engine
    if _engine is None and DATABASE_URL:
        _engine = make_engine(DATABASE_URL, echo=False, pool_pre_ping=True)
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession] | None:
    global _session_factory
    if _session_factory is None:
        eng = get_engine()
        if eng is not None:
            _session_factory = async_sessionmaker(eng, expire_on_commit=False)
    return _session_factory


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    factory = get_session_factory()
    if factory is None:
        raise RuntimeError("DATABASE_URL not configured")
    async with factory() as session:
        yield session
