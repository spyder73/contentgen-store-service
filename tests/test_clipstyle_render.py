"""Phase 4 render support in the store: migration 0038 (visibility, frozen render bundles) and the
hidden bundle media kind. HTTP tests mock the store layer; store tests run the real queries on an
in-memory sqlite database (as test_clipstyle_package_metadata.py does)."""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
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
from app.schemas import ClipstyleRenderBundleIn, ClipstyleRenderBundleOut  # noqa: E402
from app.stores import clipstyle_packages as cp  # noqa: E402
from app.stores import clipstyle_render as render_store  # noqa: E402
from app.stores import media as media_store  # noqa: E402

SECRET = "test-secret-render"
OWNER = "00000000-0000-0000-0000-000000000001"
SHA = "a" * 64
REF = "remotion-4.0.469-0123456789abcdef"


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_SECRET", SECRET)


async def _mock_session():
    yield MagicMock()


@pytest.fixture()
def client():
    app = create_fastapi_app()
    app.dependency_overrides[get_session] = _mock_session
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


def _create_tables(sync_conn):
    for table in (RenderTemplate.__table__, ClipstyleRenderBundle.__table__):
        ddl = str(CreateTable(table).compile(dialect=sync_conn.dialect))
        sync_conn.exec_driver_sql(ddl.replace("'{}'::jsonb", "'{}'"))


def _row(id_, *, status="approved", style="miniCard", version=1, user_id=OWNER, visibility="private", extra=None):
    now = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
    config = {"manifest": {"id": style, "version": version, "kind": "carousel"}, "files": {"manifest.ts": "m"}, **(extra or {})}
    return dict(id=id_, user_id=user_id, name=id_, kind="clipstyle_package", source="agent_generated", status=status,
                visibility=visibility, config=config, created_at=now, updated_at=now)


def _run(rows, fn):
    async def go():
        engine = create_async_engine("sqlite+aiosqlite://")
        async with engine.begin() as conn:
            await conn.run_sync(_create_tables)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as s:
            for r in rows:
                s.add(RenderTemplate(**r))
            await s.commit()
        try:
            async with factory() as s:
                return await fn(s)
        finally:
            await engine.dispose()

    return asyncio.run(go())


def _bundle(sha=SHA, media_id="m-1", image_ref=REF):
    return ClipstyleRenderBundleIn(bundle_sha256=sha, media_id=media_id, image_ref=image_ref)


class TestAddBundle:
    def test_freezes_the_bundle_of_an_approved_version(self):
        async def fn(s):
            out = await render_store.add_bundle(s, "tpl-1", _bundle())
            return out, await render_store.list_bundles(s, "tpl-1")

        out, listed = _run([_row("tpl-1")], fn)
        assert (out.template_id, out.bundle_sha256, out.media_id, out.image_ref) == ("tpl-1", SHA, "m-1", REF)
        assert [b.image_ref for b in listed] == [REF]

    def test_the_same_bundle_again_is_idempotent(self):
        async def fn(s):
            first = await render_store.add_bundle(s, "tpl-1", _bundle())
            again = await render_store.add_bundle(s, "tpl-1", _bundle())
            return first, again, await render_store.list_bundles(s, "tpl-1")

        first, again, listed = _run([_row("tpl-1")], fn)
        assert again.created_at == first.created_at and len(listed) == 1

    def test_another_bundle_for_the_same_image_is_refused(self):
        async def fn(s):
            await render_store.add_bundle(s, "tpl-1", _bundle())
            await render_store.add_bundle(s, "tpl-1", _bundle(sha="b" * 64, media_id="m-2"))

        with pytest.raises(ValueError, match="bundle_frozen"):
            _run([_row("tpl-1")], fn)

    def test_a_new_image_adds_a_row(self):
        async def fn(s):
            await render_store.add_bundle(s, "tpl-1", _bundle())
            await render_store.add_bundle(s, "tpl-1", _bundle(sha="b" * 64, media_id="m-2", image_ref="remotion-4.0.470-fedcba9876543210"))
            return await render_store.list_bundles(s, "tpl-1")

        assert [b.media_id for b in _run([_row("tpl-1")], fn)] == ["m-1", "m-2"]

    @pytest.mark.parametrize("status", ["draft", "validated", "rejected", "archived"])
    def test_only_an_approved_package_takes_a_bundle(self, status):
        with pytest.raises(ValueError, match="package_not_approved"):
            _run([_row("tpl-1", status=status)], lambda s: render_store.add_bundle(s, "tpl-1", _bundle()))

    def test_an_unknown_or_non_package_row_is_none(self):
        carousel = {**_row("car"), "kind": "carousel"}
        assert _run([carousel], lambda s: render_store.add_bundle(s, "car", _bundle())) is None
        assert _run([], lambda s: render_store.add_bundle(s, "nope", _bundle())) is None

    @pytest.mark.parametrize("field,value", [
        ("bundle_sha256", "A" * 64), ("bundle_sha256", "a" * 63), ("media_id", "../x"),
        ("media_id", ""), ("image_ref", " x"), ("image_ref", "x/y"),
    ])
    def test_rejects_malformed_fields(self, field, value):
        body = {"bundle_sha256": SHA, "media_id": "m-1", "image_ref": REF, field: value}
        with pytest.raises(ValidationError):
            ClipstyleRenderBundleIn(**body)


def _out():
    return ClipstyleRenderBundleOut(template_id="tpl-1", bundle_sha256=SHA, media_id="m-1", image_ref=REF,
                                    created_at=datetime(2026, 10, 10, tzinfo=timezone.utc))


class TestBundleRoutes:
    URL = "/v1/internal/clipstyle-packages/tpl-1/bundles"
    BODY = {"bundle_sha256": SHA, "media_id": "m-1", "image_ref": REF}

    def test_post_needs_only_the_internal_secret(self, client):
        with patch("app.stores.clipstyle_render.add_bundle", new=AsyncMock(return_value=_out())) as add:
            resp = client.post(self.URL, json=self.BODY, headers={"X-Internal-Secret": SECRET})
        assert resp.status_code == 200 and resp.json()["template_id"] == "tpl-1"
        assert add.call_args.args[1] == "tpl-1"

    @pytest.mark.parametrize("code", ["bundle_frozen", "package_not_approved"])
    def test_post_maps_refusals_to_409(self, client, code):
        with patch("app.stores.clipstyle_render.add_bundle", new=AsyncMock(side_effect=ValueError(code))):
            resp = client.post(self.URL, json=self.BODY, headers={"X-Internal-Secret": SECRET})
        assert resp.status_code == 409 and resp.json()["detail"] == code

    def test_post_unknown_package_is_404(self, client):
        with patch("app.stores.clipstyle_render.add_bundle", new=AsyncMock(return_value=None)):
            resp = client.post(self.URL, json=self.BODY, headers={"X-Internal-Secret": SECRET})
        assert resp.status_code == 404

    def test_post_malformed_body_is_422(self, client):
        resp = client.post(self.URL, json={**self.BODY, "bundle_sha256": "x"}, headers={"X-Internal-Secret": SECRET})
        assert resp.status_code == 422

    def test_get_lists(self, client):
        with patch("app.stores.clipstyle_render.list_bundles", new=AsyncMock(return_value=[_out()])):
            resp = client.get(self.URL, headers={"X-Internal-Secret": SECRET})
        assert resp.status_code == 200 and [b["media_id"] for b in resp.json()] == ["m-1"]

    def test_routes_need_the_secret(self, client):
        assert client.get(self.URL).status_code == 401
        assert client.post(self.URL, json=self.BODY).status_code == 401


class TestBundleMedia:
    def test_bundle_media_are_hidden_like_previews(self):
        assert media_store.CLIPSTYLE_BUNDLE_KIND == "clipstyle_bundle"
        assert set(media_store.BRIDGE_MEDIA_KINDS) == {"clipstyle_preview", "clipstyle_bundle"}

    def test_a_replaced_report_drops_its_bundle_media_too(self):
        old = {"preview_media": {"contact_sheet": {"media_id": "p-1"}, "previews": []},
               "render_bundle": {"sha256": SHA, "media_id": "b-1", "image_ref": REF}}
        new = {"preview_media": {"contact_sheet": None, "previews": []},
               "render_bundle": {"sha256": SHA, "media_id": "b-2", "image_ref": REF}}

        class _Nested:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        session = MagicMock()
        session.begin_nested = MagicMock(return_value=_Nested())
        with patch("app.stores.media.delete_clipstyle_preview_media", new=AsyncMock(return_value=2)) as delete:
            asyncio.run(cp.drop_stale_preview_media(session, "tpl-1", OWNER, old, new))
        assert set(delete.call_args.args[2]) == {"p-1", "b-1"}

    def test_a_malformed_render_bundle_key_names_no_media(self):
        assert cp._bridge_media_ids({"render_bundle": {"media_id": "../evil"}}) == []
        assert cp._bridge_media_ids({"render_bundle": "x"}) == []


def test_migration_0038_is_additive_and_its_downgrade_refuses_while_bundles_exist():
    root = Path(__file__).resolve().parent.parent
    env = {**os.environ, "DATABASE_URL": "postgresql://offline/offline"}
    up = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "0037:0038", "--sql"], cwd=root, env=env,
                        capture_output=True, text=True, check=True).stdout
    assert "ADD COLUMN visibility TEXT DEFAULT 'private' NOT NULL" in up
    assert "visibility IN ('private','published','revoked')" in up
    assert "CREATE TABLE clipstyle_render_bundles" in up
    assert "clipstyle_render_bundles is append-only" in up
    down = subprocess.run([sys.executable, "-m", "alembic", "downgrade", "0038:0037", "--sql"], cwd=root, env=env,
                          capture_output=True, text=True, check=True).stdout
    assert down.index("RAISE EXCEPTION") < down.index("DROP TABLE clipstyle_render_bundles")
