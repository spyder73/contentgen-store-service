"""Telegram recipients: the chat ids a user may send to, and the ones a chat sends to (migration 0057).

Every function is scoped by ``user_id``; a refusal raises ``ChatError`` with its HTTP status and code. At most
``RECIPIENT_LIMIT`` per user, enforced under the user's row lock so two puts cannot both take the last place.
Removing a recipient also removes its id from that user's threads' ``telegram_targets`` in the same commit.
"""

from __future__ import annotations

import uuid

from sqlalchemy import Text, cast, delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import ChatThread, TelegramRecipient
from ..schemas import TelegramRecipientIn
from .chat import ChatError
from .credits import _lock_user_row

RECIPIENT_LIMIT = 20


async def list_for_user(session: AsyncSession, user_id: str) -> list[TelegramRecipient]:
    query = (
        select(TelegramRecipient)
        .where(TelegramRecipient.user_id == user_id)
        .order_by(TelegramRecipient.created_at, TelegramRecipient.id)
    )
    return list((await session.execute(query)).scalars())


async def put(session: AsyncSession, user_id: str, telegram_id: str, payload: TelegramRecipientIn) -> TelegramRecipient:
    """Add the recipient, or rename the one already there."""
    await _lock_user_row(session, user_id)
    rows = await list_for_user(session, user_id)
    row = next((r for r in rows if r.telegram_id == telegram_id), None)
    if row is None:
        if len(rows) >= RECIPIENT_LIMIT:
            await session.rollback()
            raise ChatError(409, "recipient_limit")
        row = TelegramRecipient(id=str(uuid.uuid4()), user_id=user_id, telegram_id=telegram_id)
        session.add(row)
    row.username = payload.username
    await session.commit()
    await session.refresh(row)
    return row


async def delete_one(session: AsyncSession, user_id: str, telegram_id: str) -> bool:
    removed = await session.execute(
        delete(TelegramRecipient).where(
            TelegramRecipient.user_id == user_id, TelegramRecipient.telegram_id == telegram_id
        )
    )
    if removed.rowcount == 0:
        await session.rollback()
        return False
    # The text match only narrows the rows; the exact filter is below (portable across sqlite and postgres).
    threads = await session.execute(
        select(ChatThread.id, ChatThread.telegram_targets).where(
            ChatThread.user_id == user_id, cast(ChatThread.telegram_targets, Text).like(f'%"{telegram_id}"%')
        )
    )
    for thread_id, targets in threads.all():
        if telegram_id in targets:
            await session.execute(
                update(ChatThread)
                .where(ChatThread.id == thread_id)
                .values(telegram_targets=[t for t in targets if t != telegram_id], updated_at=ChatThread.updated_at)
                .execution_options(synchronize_session=False)
            )
    await session.commit()
    return True
