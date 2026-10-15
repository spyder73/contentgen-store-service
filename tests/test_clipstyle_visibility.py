"""Phase 4 publishing in the store: the owner's and the admin's visibility writers, the usable list
and the admin catalog. Store tests run on in-memory sqlite like test_clipstyle_render.py."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
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


from app.db import get_session  # noqa: E402
from app.fastapi_app import create_fastapi_app  # noqa: E402
from app.models import ClipstyleRenderBundle, RenderTemplate  # noqa: E402
from app.stores import clipstyle_render as store  # noqa: E402

OWNER = "00000000-0000-0000-0000-000000000001"
OTHER = "00000000-0000-0000-0000-000000000002"
T0 = datetime(2026, 10, 10, tzinfo=timezone.utc)
SECRET = "test-secret-visibility"


async def _mock_session():
    yield MagicMock()


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_SECRET", SECRET)


@pytest.fixture()
def client():
    app = create_fastapi_app()
    app.dependency_overrides[get_session] = _mock_session
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


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


def test_an_owner_publish_races_an_admin_revoke_across_two_sessions_and_loses():
    # Re-review round 2, I-2: a single shared AsyncSession (as the round-1 test used) can't
    # expose the lost-update race — both calls see the same in-process identity map, so the
    # second call's read is never stale regardless of which code is under test. A genuine
    # regression needs two independent sessions, matching two independent request-scoped
    # sessions in production:
    #   1. Session A (the owner) loads the row first, into its OWN identity map, while it is
    #      still "private" — exactly what the old session.get -> mutate -> commit code did as
    #      its first line.
    #   2. Session B (the admin) revokes and commits, independently of A.
    #   3. Session A then calls set_visibility("published") on its own (now stale) session.
    #   4. The persisted row must stay "revoked", and A's call must be refused.
    # Against the pre-fix code (44ede86), step 3's `session.get` returns A's cached pre-revoke
    # object (SQLAlchemy's identity map short-circuits the query), so the old code sees
    # visibility="private", never raises, and overwrites B's revoke with "published" — proven by
    # running this exact scenario against a copy of the pre-fix function (see task-4.1-report.md,
    # "Fix round 2"): it raises nothing and persists "published". The fixed code's UPDATE ...
    # WHERE visibility != 'revoked' is evaluated by the database against the current row, not
    # against A's cached copy, so it correctly matches 0 rows and refuses.
    async def go():
        engine = create_async_engine("sqlite+aiosqlite://")
        async with engine.begin() as conn:
            def ddl(sync_conn):
                for table in (RenderTemplate.__table__, ClipstyleRenderBundle.__table__):
                    sync_conn.exec_driver_sql(str(CreateTable(table).compile(dialect=sync_conn.dialect)).replace("'{}'::jsonb", "'{}'"))
            await conn.run_sync(ddl)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as seed:
            seed.add(_row("v1"))
            await seed.commit()

        session_a = factory()
        session_b = factory()
        try:
            row_a = await session_a.get(RenderTemplate, "v1")  # 1: A's own, now-stale, read.
            assert row_a.visibility == "private"

            revoked = await store.set_visibility(session_b, "v1", "revoked", admin=True)  # 2
            assert revoked == {"id": "v1", "visibility": "revoked"}

            with pytest.raises(ValueError, match="package_revoked"):  # 3
                await store.set_visibility(session_a, "v1", "published", owner=OWNER)
        finally:
            await session_a.close()
            await session_b.close()

        async with factory() as verify:  # 4: never overwritten.
            row = await verify.get(RenderTemplate, "v1", populate_existing=True)
            persisted = row.visibility
        await engine.dispose()
        return persisted

    assert asyncio.run(go()) == "revoked"


class TestAdminVisibilityRouteRequiresAVerifiedAdmin:
    """Review round 1, I-1: the admin route follows its sibling /v1/internal/admin/* routes (e.g.
    admin_pipeline_visibility_handler) — internal secret AND a verified admin, not just any holder
    of the shared secret."""

    URL = "/v1/internal/admin/render-templates/v1/visibility"

    def test_non_admin_caller_is_refused(self, client):
        with patch("app.stores.credits.is_admin", new=AsyncMock(return_value=False)):
            resp = client.patch(
                self.URL,
                json={"visibility": "revoked"},
                headers={"X-Internal-Secret": SECRET, "X-User-ID": OWNER},
            )
        assert resp.status_code == 403
        assert resp.json()["detail"] == "not_admin"

    def test_a_verified_admin_succeeds(self, client):
        set_visibility = AsyncMock(return_value={"id": "v1", "visibility": "revoked"})
        with patch("app.stores.credits.is_admin", new=AsyncMock(return_value=True)), \
             patch("app.stores.clipstyle_render.set_visibility", new=set_visibility):
            resp = client.patch(
                self.URL,
                json={"visibility": "revoked"},
                headers={"X-Internal-Secret": SECRET, "X-User-ID": OWNER},
            )
        assert resp.status_code == 200
        assert resp.json() == {"id": "v1", "visibility": "revoked"}
        set_visibility.assert_awaited_once_with(ANY, "v1", "revoked", admin=True)

    def test_needs_the_internal_secret_even_before_the_admin_check(self, client):
        resp = client.patch(self.URL, json={"visibility": "revoked"}, headers={"X-User-ID": OWNER})
        assert resp.status_code == 401
