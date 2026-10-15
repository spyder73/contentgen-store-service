"""Phase 4 publishing in the store: the owner's and the admin's visibility writers, the usable list
and the admin catalog. Store tests run on in-memory sqlite like test_clipstyle_render.py."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.schema import CreateTable


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(element, compiler, **kw):  # pragma: no cover - trivial
    return "JSON"


@compiles(UUID, "sqlite")
def _uuid_sqlite(element, compiler, **kw):  # pragma: no cover - trivial
    return "VARCHAR(36)"


from app.models import ClipstyleRenderBundle, RenderTemplate  # noqa: E402
from app.stores import clipstyle_render as store  # noqa: E402

OWNER = "00000000-0000-0000-0000-000000000001"
OTHER = "00000000-0000-0000-0000-000000000002"
T0 = datetime(2026, 10, 10, tzinfo=timezone.utc)


def _row(id_, *, style="neonKit", version=1, user_id=OWNER, status="approved", visibility="private", age=0):
    config = {"manifest": {"id": style, "version": version, "kind": "carousel"}, "files": {"manifest.ts": "m"}, "implements": "technologyCarousel@1"}
    return RenderTemplate(id=id_, user_id=user_id, name=id_, kind="clipstyle_package", source="agent_generated", status=status,
                          visibility=visibility, config=config, approved_at=T0 - timedelta(days=age) if status == "approved" else None,
                          created_at=T0, updated_at=T0)


def _run(rows, fn):
    async def go():
        engine = create_async_engine("sqlite+aiosqlite://")
        async with engine.begin() as conn:
            def ddl(sync_conn):
                for table in (RenderTemplate.__table__, ClipstyleRenderBundle.__table__):
                    sync_conn.exec_driver_sql(str(CreateTable(table).compile(dialect=sync_conn.dialect)).replace("'{}'::jsonb", "'{}'"))
            await conn.run_sync(ddl)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as s:
            s.add_all(rows)
            await s.commit()
        try:
            async with factory() as s:
                return await fn(s)
        finally:
            await engine.dispose()

    return asyncio.run(go())


def test_only_the_owner_toggles_private_and_published():
    async def fn(s):
        out = await store.set_visibility(s, "v1", "published", owner=OWNER)
        assert out == {"id": "v1", "visibility": "published"}
        assert await store.set_visibility(s, "v1", "published", owner=OTHER) is None
        return (await store.set_visibility(s, "v1", "private", owner=OWNER))["visibility"]

    assert _run([_row("v1")], fn) == "private"


def test_an_unapproved_version_cannot_be_published():
    async def fn(s):
        with pytest.raises(ValueError, match="package_not_approved"):
            await store.set_visibility(s, "d1", "published", owner=OWNER)

    _run([_row("d1", status="validated")], fn)


def test_revoked_is_admin_only_and_blocks_the_owner_too():
    async def fn(s):
        with pytest.raises(ValueError, match="invalid_visibility"):
            await store.set_visibility(s, "v1", "revoked", owner=OWNER)
        await store.set_visibility(s, "v1", "revoked", admin=True)
        with pytest.raises(ValueError, match="package_revoked"):
            await store.set_visibility(s, "v1", "published", owner=OWNER)
        with pytest.raises(ValueError, match="invalid_visibility"):
            await store.set_visibility(s, "v1", "published", admin=True)
        assert (await store.set_visibility(s, "v1", "private", admin=True))["visibility"] == "private"
        return [u.template_id for u in await store.list_usable(s, caller=OWNER)]

    assert _run([_row("v1")], fn) == ["v1"]


def test_usable_is_own_unrevoked_plus_everyones_published():
    rows = [_row("mine", age=3), _row("mine-revoked", style="oldKit", visibility="revoked", age=2),
            _row("theirs-private", style="otherKit", user_id=OTHER, age=1), _row("theirs-published", style="pubKit", user_id=OTHER, visibility="published"),
            _row("mine-draft", style="draftKit", status="validated")]

    async def fn(s):
        usable = await store.list_usable(s, caller=OWNER)
        return [(u.template_id, u.caller_is_owner, u.implements) for u in usable]

    assert _run(rows, fn) == [("theirs-published", False, "technologyCarousel@1"), ("mine", True, "technologyCarousel@1")]


def test_a_clone_of_a_revoked_version_stays_revoked():
    from app.schemas import RenderTemplateCloneIn
    from app.stores import render_templates

    async def fn(s):
        out = await render_templates.clone_template(s, "v1", RenderTemplateCloneIn(id="v2"), user_id=OWNER)
        row = await s.get(RenderTemplate, out.id)
        return row.visibility, row.status

    # M15: "New version" → re-approve → publish cannot route around an admin's revoke.
    assert _run([_row("v1", visibility="revoked")], fn) == ("revoked", "draft")


def test_the_catalog_lists_every_approved_version_without_files():
    async def fn(s):
        return [c.model_dump() for c in await store.list_catalog(s)]

    out = _run([_row("v1", visibility="published"), _row("d1", status="draft")], fn)
    assert [c["template_id"] for c in out] == ["v1"] and "config" not in out[0] and out[0]["owner"] == OWNER
