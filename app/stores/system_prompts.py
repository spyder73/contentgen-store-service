from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from ..models import SystemPrompt


async def get_system_prompt(session: AsyncSession, id: str) -> str | None:
    row = await session.get(SystemPrompt, id)
    return row.content if row is not None else None


async def upsert_system_prompt(session: AsyncSession, id: str, content: str) -> str:
    row = await session.get(SystemPrompt, id)
    if row is None:
        row = SystemPrompt(id=id)
        session.add(row)
    row.content = content
    await session.commit()
    await session.refresh(row)
    return row.content
