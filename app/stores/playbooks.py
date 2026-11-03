"""Playbooks: a user's copies and edits of the chat agent's Markdown how-tos (migration 0046).

Every function is scoped by ``user_id`` as ``chat.py`` is: another user's row is simply not
found (``None``), and a refusal the caller must tell apart raises ``ChatError`` carrying the
HTTP status and a stable code.

A save is one transaction, and ``expect_version`` is decided in one place. ``expect_version``
0 inserts the playbook at version 1 (at version 2, over the seed's version 1, when the save
carries a seed), and the unique ``(user_id, slug)`` refuses it when the playbook exists. A higher
one is the guard of the UPDATE that moves the playbook to the next version, so a stale caller
matches no row and writes nothing, however the writers interleave. The version rows are added in
the same transaction as the insert or update, never on their own.
"""

from __future__ import annotations

import re
import uuid

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Playbook, PlaybookVersion
from ..schemas import PlaybookIn
from .chat import ChatError, _now

SLUG = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
STALE = "stale_playbook_version"


async def list_playbooks(session: AsyncSession, user_id: str) -> list[Playbook]:
    """The user's own rows by slug; the shipped files are the backend's."""
    query = select(Playbook).where(Playbook.user_id == user_id).order_by(Playbook.slug)
    return list((await session.execute(query)).scalars())


async def get_playbook(session: AsyncSession, user_id: str, slug: str) -> Playbook | None:
    return (
        await session.execute(
            select(Playbook)
            .where(Playbook.user_id == user_id, Playbook.slug == slug)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def list_versions(session: AsyncSession, user_id: str, slug: str) -> list[PlaybookVersion] | None:
    """The playbook's saves, newest first; ``None`` when the user has no such playbook."""
    playbook = await get_playbook(session, user_id, slug)
    if playbook is None:
        return None
    query = (
        select(PlaybookVersion)
        .where(PlaybookVersion.playbook_id == playbook.id)
        .order_by(PlaybookVersion.version.desc())
    )
    return list((await session.execute(query)).scalars())


async def get_version(session: AsyncSession, user_id: str, slug: str, version: int) -> PlaybookVersion | None:
    playbook = await get_playbook(session, user_id, slug)
    if playbook is None:
        return None
    return (
        await session.execute(
            select(PlaybookVersion).where(
                PlaybookVersion.playbook_id == playbook.id, PlaybookVersion.version == version
            )
        )
    ).scalar_one_or_none()


def _version_row(playbook_id: str, version: int, payload: PlaybookIn) -> PlaybookVersion:
    return PlaybookVersion(
        id=str(uuid.uuid4()), playbook_id=playbook_id, version=version,
        body=payload.body, note=payload.note, author=payload.author,
    )


async def save_playbook(session: AsyncSession, user_id: str, slug: str, payload: PlaybookIn) -> Playbook:
    """Save the playbook as the next version, or raise 409 ``stale_playbook_version`` when
    ``payload.expect_version`` is not the stored version (0 meaning there is none)."""
    if not SLUG.fullmatch(slug):
        raise ChatError(422, "invalid_slug")
    if payload.expect_version == 0:
        return await _create(session, user_id, slug, payload)
    return await _update(session, user_id, slug, payload)


async def _create(session: AsyncSession, user_id: str, slug: str, payload: PlaybookIn) -> Playbook:
    seeded = payload.seed_body is not None
    version = 2 if seeded else 1
    row = Playbook(
        id=str(uuid.uuid4()),
        user_id=user_id,
        slug=slug,
        name=payload.name,
        description=payload.description,
        body=payload.body,
        version=version,
        based_on_shipped_version=payload.based_on_shipped_version,
    )
    session.add(row)
    if seeded:
        session.add(
            PlaybookVersion(
                id=str(uuid.uuid4()), playbook_id=row.id, version=1,
                body=payload.seed_body, note=payload.seed_note or "", author="user",
            )
        )
    session.add(_version_row(row.id, version, payload))
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        # The unique (user_id, slug) refused: the playbook exists, so the caller's 0 is stale.
        if await get_playbook(session, user_id, slug) is not None:
            raise ChatError(409, STALE)
        raise
    await session.refresh(row)
    return row


async def _update(session: AsyncSession, user_id: str, slug: str, payload: PlaybookIn) -> Playbook:
    values = {
        "name": payload.name,
        "description": payload.description,
        "body": payload.body,
        "version": Playbook.version + 1,
        "updated_at": _now(),
    }
    if payload.based_on_shipped_version is not None:
        values["based_on_shipped_version"] = payload.based_on_shipped_version
    # The guard: only the stored version the caller read matches, and only one writer can move it.
    row = (
        await session.execute(
            update(Playbook)
            .where(Playbook.user_id == user_id, Playbook.slug == slug, Playbook.version == payload.expect_version)
            .values(**values)
            .returning(Playbook)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if row is None:
        await session.rollback()
        raise ChatError(409, STALE)
    session.add(_version_row(row.id, payload.expect_version + 1, payload))
    await session.commit()
    await session.refresh(row)
    return row
