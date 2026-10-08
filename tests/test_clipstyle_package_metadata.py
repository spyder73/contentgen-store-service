"""GET /v1/clipstyle-packages/metadata: the caller's clipstyle package rows without their files.

A clipstyle package row's ``config`` carries up to 512 KiB of package files. The Go backend's
owner-only ``GET /clipstyles`` filter needs only each row's id and owner, so this list answers
id/owner/name/status plus the manifest's style id and version, extracted in SQL: the files
never leave the database (#3 Task 19 carry 5). The endpoint is additive; no existing route
changes.

The HTTP tests mock the store layer like the rest of the suite. The store tests run the real
query on an in-memory sqlite database, with sqlite renderings for the Postgres-only column
types (as ``test_media_list_deferred_blob.py`` does); render_templates' ``'{}'::jsonb`` server
default is Postgres syntax, so the sqlite table is created from the compiled DDL with that
default swapped for its sqlite spelling.
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql
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


from app.db import get_session  # noqa: E402  (after compiler registration)
from app.fastapi_app import create_fastapi_app  # noqa: E402
from app.models import RenderTemplate  # noqa: E402
from app.schemas import ClipstylePackageMetadataOut  # noqa: E402
from app.stores import clipstyle_package_metadata as metadata_store  # noqa: E402

INTERNAL_SECRET = "test-secret-xyz"
OWNER = "00000000-0000-0000-0000-000000000001"
OTHER = "00000000-0000-0000-0000-000000000002"


async def _mock_session():
    yield MagicMock()


@pytest.fixture(autouse=True)
def _internal_secret_env(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_SECRET", INTERNAL_SECRET)


@pytest.fixture()
def client():
    app = create_fastapi_app()
    app.dependency_overrides[get_session] = _mock_session
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


def _headers(user_id: str | None) -> dict[str, str]:
    headers = {"X-Internal-Secret": INTERNAL_SECRET}
    if user_id:
        headers["X-User-ID"] = user_id
    return headers


def _meta(id_: str, **overrides) -> ClipstylePackageMetadataOut:
    now = datetime.now(timezone.utc)
    data = {"id": id_, "user_id": OWNER, "name": "Captions", "status": "validated", "style_id": "imageSequenceCaptions",
            "version": 2, "created_at": now, "updated_at": now}
    data.update(overrides)
    return ClipstylePackageMetadataOut(**data)


class TestClipstylePackageMetadataRoute:
    def test_lists_the_callers_rows_as_metadata_only(self, client):
        with patch(
            "app.stores.clipstyle_package_metadata.list_package_metadata",
            new=AsyncMock(return_value=[_meta("tpl-1")]),
        ) as m:
            resp = client.get("/v1/clipstyle-packages/metadata", headers=_headers(OWNER))
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 1
        assert set(body[0]) == {"id", "user_id", "name", "status", "style_id", "version", "created_at", "updated_at"}
        assert body[0]["version"] == 2 and body[0]["style_id"] == "imageSequenceCaptions"
        assert m.call_args.kwargs == {"user_id": OWNER, "include_archived": False}

    def test_forwards_include_archived(self, client):
        with patch(
            "app.stores.clipstyle_package_metadata.list_package_metadata",
            new=AsyncMock(return_value=[]),
        ) as m:
            resp = client.get(
                "/v1/clipstyle-packages/metadata", headers=_headers(OWNER), params={"include_archived": "true"}
            )
        assert resp.status_code == 200
        assert m.call_args.kwargs == {"user_id": OWNER, "include_archived": True}

    def test_requires_a_user(self, client):
        with patch(
            "app.stores.clipstyle_package_metadata.list_package_metadata",
            new=AsyncMock(return_value=[]),
        ) as m:
            resp = client.get("/v1/clipstyle-packages/metadata", headers=_headers(None))
        assert resp.status_code == 401
        assert resp.json()["detail"] == "user_id_required"
        m.assert_not_called()

    def test_requires_the_internal_secret(self, client):
        resp = client.get("/v1/clipstyle-packages/metadata", headers={"X-User-ID": OWNER})
        assert resp.status_code == 401

    def test_the_existing_render_template_routes_are_unchanged(self, client):
        # The new route lives outside /v1/render-templates, so GET /v1/render-templates/{id} still
        # answers every id it did, including one spelled "metadata".
        with patch(
            "app.stores.render_templates.get_template", new=AsyncMock(return_value=None)
        ) as m:
            resp = client.get("/v1/render-templates/metadata", headers=_headers(OWNER))
        assert resp.status_code == 404
        assert m.call_args.args[1] == "metadata"


# ── the query, on a real (sqlite) database ──────────────────────────────────

_BIG_FILES = {"Template.tsx": "x" * (600 << 10), "schema.ts": "export const schema = {}"}


def _create_render_templates(sync_conn):
    ddl = str(CreateTable(RenderTemplate.__table__).compile(dialect=sync_conn.dialect))
    sync_conn.exec_driver_sql(ddl.replace("'{}'::jsonb", "'{}'"))


async def _factory_with(rows: list[dict]):
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(_create_render_templates)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        for r in rows:
            s.add(RenderTemplate(**r))
        await s.commit()
    return engine, factory


def _row(id_: str, *, user_id: str | None = OWNER, kind: str = "clipstyle_package", status: str = "draft",
         config: dict | None = None, minutes_ago: int = 0) -> dict:
    created = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc) - timedelta(minutes=minutes_ago)
    return dict(id=id_, user_id=user_id, name=f"name-{id_}", kind=kind, source="agent_generated", status=status,
                config=config if config is not None else {}, created_at=created, updated_at=created)


def _package_config(style: str, version) -> dict:
    return {"manifest": {"id": style, "version": version, "kind": "carousel"}, "files": _BIG_FILES,
            "clipstyle_session_id": "s-1"}


def _list(rows: list[dict], **kwargs) -> list[ClipstylePackageMetadataOut]:
    async def run():
        engine, factory = await _factory_with(rows)
        try:
            async with factory() as s:
                return await metadata_store.list_package_metadata(s, **kwargs)
        finally:
            await engine.dispose()

    return asyncio.run(run())


class TestListPackageMetadata:
    # Statuses within the live schema's CHECK (draft/active/archived); the package lifecycle's own
    # statuses (validated, approved, ...) arrive with that branch, and the query passes any through.
    ROWS = [
        _row("own-new", status="active", config=_package_config("captions", 3), minutes_ago=1),
        _row("own-old", status="draft", config=_package_config("captions", 2), minutes_ago=5),
        _row("own-archived", status="archived", config=_package_config("captions", 1), minutes_ago=9),
        _row("other-users", user_id=OTHER, config=_package_config("theirs", 1)),
        _row("system-package", user_id=None, config=_package_config("system", 1)),
        _row("own-carousel", kind="carousel", status="active", config={"layout": "split"}),
    ]

    def test_only_the_callers_own_package_rows_newest_first(self):
        out = _list(self.ROWS, user_id=OWNER)
        assert [m.id for m in out] == ["own-new", "own-old"]
        assert all(m.user_id == OWNER for m in out)
        assert (out[0].name, out[0].status, out[0].style_id, out[0].version) == ("name-own-new", "active", "captions", 3)
        assert (out[1].status, out[1].version) == ("draft", 2)

    def test_include_archived(self):
        out = _list(self.ROWS, user_id=OWNER, include_archived=True)
        assert [m.id for m in out] == ["own-new", "own-old", "own-archived"]

    def test_never_carries_a_file(self):
        for m in _list(self.ROWS, user_id=OWNER, include_archived=True):
            dumped = m.model_dump()
            assert set(dumped) == {"id", "user_id", "name", "status", "style_id", "version", "created_at", "updated_at"}
            assert "export const schema" not in str(dumped) and "xxxx" not in str(dumped)

    def test_a_missing_or_malformed_manifest_answers_no_version(self):
        rows = [
            _row("no-manifest", config={}, minutes_ago=1),
            _row("text-version", config=_package_config("a", "7"), minutes_ago=2),
            _row("bad-version", config=_package_config("b", "seven"), minutes_ago=3),
            _row("float-version", config=_package_config("d", 1.5), minutes_ago=5),
        ]
        got = {m.id: (m.style_id, m.version) for m in _list(rows, user_id=OWNER)}
        assert got == {
            "no-manifest": (None, None),
            "text-version": ("a", 7),
            "bad-version": ("b", None),
            "float-version": ("d", None),
        }

    def test_version_parsing(self):
        # What the two databases hand back for config.manifest.version: Postgres' #>> is text
        # ("3", "true", "1.5"); sqlite's JSON_EXTRACT a native value. Only an integer is a version.
        cases = {3: 3, "3": 3, " 12 ": 12, "-1": -1, True: None, "true": None, 1.5: None, "1.5": None,
                 "seven": None, "--1": None, "1e3": None, "": None, None: None}
        for raw, want in cases.items():
            assert metadata_store._version(raw) == want, raw

    def test_the_postgres_query_extracts_the_manifest_and_never_selects_config(self):
        sql = str(metadata_store.metadata_query(user_id=OWNER).compile(dialect=postgresql.dialect()))
        select_list = sql.split(" FROM ", 1)[0]
        assert select_list.count("render_templates.config #>>") == 2
        # config appears only inside the two path extractions, never as a column of its own.
        assert re.findall(r"render_templates\.config(?!\s*#>>)", select_list) == []
        assert "render_templates.kind = " in sql and "render_templates.user_id = " in sql
