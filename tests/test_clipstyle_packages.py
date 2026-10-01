from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.db import get_session
from app.fastapi_app import create_fastapi_app
from app.schemas import RenderTemplateCloneIn, RenderTemplateIn, RenderTemplateOut
from app.stores import clipstyle_packages as cp

SECRET = "test-secret-xyz"
UID = "00000000-0000-0000-0000-000000000001"
OTHER_UID = "00000000-0000-0000-0000-000000000009"
FILES = {"manifest.ts": "export const manifest = 1;\n", "fixtures/a.json": '{"x":1}\n'}
SHA = "fcd4c6245b9417e2da395808bb48a03a07da263bcf834aaeb87a70ebb637a93c"
CONFIG = {"manifest": {"id": "miniCard", "version": 1, "kind": "carousel"}, "files": FILES}
OK = {"ok": True, "contract_version": 1, "package": {"sha256": SHA}}
# Cross-language sha vectors, a verbatim COPY of remotion/modal_bridge/tests/fixtures/package-files-vectors.json
# (generated from the real TS `packageSha256` by remotion/tests/validator-cross-language.test.ts).
# After an intended change to the TS algorithm, regenerate in remotion/ with
#   CLIPKIT_WRITE_VECTORS=1 npx tsx --test tests/validator-cross-language.test.ts
# then copy the file here again: this copy does not update itself.
VECTORS = json.loads((Path(__file__).parent / "fixtures" / "package-files-vectors.json").read_text(encoding="utf-8"))
# final fix wave M11/N4: pin the vector file's own hash so a regeneration on just one side (store or
# remotion) fails loudly here instead of the two copies silently drifting apart. The remotion copy
# pins the same value (modal_bridge/tests/test_validator_types.py VECTORS_SHA256).
VECTORS_SHA256 = "08e2986cfd43af3c233cb7172ef42843c3ba437b9e4790e91d5ff6362845f2a1"


def test_the_vector_file_is_the_one_both_repos_pin():
    path = Path(__file__).parent / "fixtures" / "package-files-vectors.json"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == VECTORS_SHA256


def test_sha_matches_the_cross_language_vector():
    assert cp.package_sha256(FILES) == SHA


def _expand(value):
    if isinstance(value, dict) and "$gen" in value:
        g = value["$gen"]
        return g["repeat"] * g["count"] + g["suffix"]
    return value


@pytest.mark.parametrize("vector", [v for v in VECTORS if v["sha256"] is not None], ids=lambda v: v["name"])
def test_sha_matches_every_typescript_vector(vector):
    files = {path: _expand(content) for path, content in vector["files"]}
    assert cp.package_sha256(files) == vector["sha256"]


def test_vectors_include_the_ordering_and_surrogate_cases():
    names = {v["name"] for v in VECTORS}
    assert {"sort-order-utf16", "sort-order-utf16-contents", "surrogate-pair-and-lone", "lone-surrogate-over-limit"} <= names


@pytest.mark.parametrize(
    "config, reason",
    [
        ({}, "manifest"),
        ({"manifest": {"id": "1x", "version": 1, "kind": "carousel"}, "files": FILES}, "manifest.id"),
        ({"manifest": {"id": "miniCard", "version": True, "kind": "carousel"}, "files": FILES}, "manifest.version"),
        ({"manifest": {"id": "miniCard", "version": 0, "kind": "carousel"}, "files": FILES}, "manifest.version"),
        ({"manifest": {"id": "miniCard", "version": "1", "kind": "carousel"}, "files": FILES}, "manifest.version"),
        ({"manifest": {"id": "miniCard", "version": 1, "kind": "gif"}, "files": FILES}, "manifest.kind"),
        ({"manifest": {"id": "miniCard", "version": 1, "kind": "video"}, "files": {}}, "files"),
        ({"manifest": {"id": "miniCard", "version": 1, "kind": "video"}, "files": {"a": 1}}, "files"),
        # M2 (final fix wave): the same path/count/size limits the runner's check_files enforces,
        # applied here at write time so a bad path or an oversized package never round-trips
        # through a full validation first.
        ({"manifest": {"id": "miniCard", "version": 1, "kind": "carousel"}, "files": {**FILES, "evil.exe": "x"}}, "files.path"),
        (
            {"manifest": {"id": "miniCard", "version": 1, "kind": "carousel"}, "files": {f"lib/f{i}.ts": "x" for i in range(cp.MAX_FILES + 1)}},
            "files.count",
        ),
        (
            {"manifest": {"id": "miniCard", "version": 1, "kind": "carousel"}, "files": {"manifest.ts": "x" * (cp.MAX_FILE_BYTES + 1)}},
            "files.size",
        ),
        (
            {
                "manifest": {"id": "miniCard", "version": 1, "kind": "carousel"},
                "files": {f"lib/big{i}.ts": "x" * cp.MAX_FILE_BYTES for i in range(9)},
            },
            "files.size",
        ),
    ],
)
def test_check_package_config_rejects(config, reason):
    with pytest.raises(ValueError, match=f"invalid_package_config:{reason}$"):
        cp.check_package_config(config)


def test_check_package_config_accepts_a_well_formed_package():
    cp.check_package_config(CONFIG)


@pytest.mark.parametrize(
    "current, target, report, expected",
    [
        ("draft", "validated", OK, "validated"),
        ("draft", "draft", {"ok": False}, "draft"),
        ("validated", "validated", OK, "validated"),
        ("validated", "approved", OK, "approved"),
        ("validated", "draft", {"ok": False}, "draft"),
        ("approved", "approved", OK, "approved"),
        ("draft", "rejected", {}, "rejected"),
        ("validated", "rejected", {}, "rejected"),
    ],
)
def test_allowed_transitions(current, target, report, expected):
    assert cp.next_package_status(current, target, report=report, files=FILES) == expected


@pytest.mark.parametrize(
    "current, target, report, error",
    [
        ("draft", "approved", OK, "invalid_transition:draft->approved"),
        ("approved", "draft", OK, "invalid_transition:approved->draft"),
        ("approved", "rejected", OK, "invalid_transition:approved->rejected"),
        ("rejected", "validated", OK, "invalid_transition:rejected->validated"),
        ("rejected", "rejected", OK, "invalid_transition:rejected->rejected"),
        ("active", "validated", OK, "invalid_transition:active->validated"),
        ("draft", "validated", {"ok": False}, "validation_report_not_ok"),
        ("draft", "validated", {"ok": "true", "contract_version": 1, "package": {"sha256": SHA}}, "validation_report_not_ok"),
        ("draft", "validated", None, "validation_report_not_ok"),
        ("draft", "validated", {"ok": True, "package": {"sha256": SHA}}, "validation_report_contract_version"),
        ("draft", "validated", {**OK, "contract_version": 2}, "validation_report_contract_version"),
        ("draft", "validated", {**OK, "contract_version": True}, "validation_report_contract_version"),
        ("validated", "approved", {**OK, "package": {"sha256": "0" * 64}}, "validation_report_stale"),
        ("validated", "approved", {**OK, "package": None}, "validation_report_stale"),
        ("draft", "published", OK, "invalid_status"),
        ("draft", "active", OK, "invalid_status"),
    ],
)
def test_refused_transitions(current, target, report, error):
    with pytest.raises(ValueError, match=error):
        cp.next_package_status(current, target, report=report, files=FILES)


def _row(**overrides):
    base = dict(id="tpl-1", user_id=UID, kind=cp.PACKAGE_KIND, status="draft", config=CONFIG, validation_report_json={}, approved_at=None, preview_url=None)
    base.update(overrides)
    return SimpleNamespace(**base)


def test_apply_package_body_forces_draft_and_clears_the_report():
    row = _row(status="validated", validation_report_json=OK)
    cp.apply_package_body(row, RenderTemplateIn(name="p", kind=cp.PACKAGE_KIND, config=CONFIG))
    assert (row.status, row.validation_report_json, row.approved_at) == ("draft", {}, None)
    with pytest.raises(ValueError, match="package_status_via_status_endpoint"):
        cp.apply_package_body(_row(), RenderTemplateIn(name="p", kind=cp.PACKAGE_KIND, status="approved", config=CONFIG))
    with pytest.raises(ValueError, match="invalid_package_config:source"):
        cp.apply_package_body(_row(), RenderTemplateIn(name="p", kind=cp.PACKAGE_KIND, source="builtin", config=CONFIG))


def test_apply_package_body_ignores_a_client_supplied_preview_and_resets_the_old_one():
    """preview_url is bridge-owned: a package body edit never lets the client set it, and any edit
    drops whatever the bridge had set before, since it described the pre-edit files."""
    row = _row(status="validated", preview_url="/v1/media/bridge-set/file")
    cp.apply_package_body(
        row, RenderTemplateIn(name="p", kind=cp.PACKAGE_KIND, config=CONFIG, preview_url="/v1/media/attacker/file")
    )
    assert row.preview_url is None


def test_guard_package_update_blocks_approved_and_rejected():
    for status in ("approved", "rejected"):
        with pytest.raises(ValueError, match="package_immutable"):
            cp.guard_package_update(_row(status=status))
    cp.guard_package_update(_row(status="validated"))
    cp.guard_package_update(SimpleNamespace(kind="carousel", status="active"))


def _session(row, others=()):
    session = MagicMock()
    session.get = AsyncMock(return_value=row)
    session.execute = AsyncMock(return_value=MagicMock(scalars=MagicMock(return_value=list(others))))
    session.commit = AsyncMock()
    session.refresh = AsyncMock()
    session.rollback = AsyncMock()
    return session


def test_set_package_status_approves_and_stamps_approved_at():
    row = _row(status="validated", validation_report_json=OK)
    with patch.object(cp, "_template_out", side_effect=lambda r: r):
        out = asyncio.run(cp.set_package_status(_session(row), "tpl-1", "approved", user_id=UID))
    assert out.status == "approved"
    assert isinstance(out.approved_at, datetime)


def test_set_package_status_refuses_a_second_approved_row_for_the_same_version():
    row = _row(status="validated", validation_report_json=OK)
    other = _row(id="tpl-0", status="approved")
    with pytest.raises(ValueError, match="package_version_already_approved"):
        asyncio.run(cp.set_package_status(_session(row, [other]), "tpl-1", "approved", user_id=UID))


def test_set_package_status_refuses_an_id_approved_by_another_user():
    row = _row(status="validated", validation_report_json=OK)
    other = _row(id="tpl-9", user_id=OTHER_UID, status="approved",
                 config={**CONFIG, "manifest": {**CONFIG["manifest"], "version": 7}})
    with pytest.raises(ValueError, match="package_id_taken"):
        asyncio.run(cp.set_package_status(_session(row, [other]), "tpl-1", "approved", user_id=UID))


def test_set_package_status_hides_other_users_and_other_kinds():
    assert asyncio.run(cp.set_package_status(_session(_row(user_id="someone-else")), "tpl-1", "validated", user_id=UID)) is None
    assert asyncio.run(cp.set_package_status(_session(_row(kind="carousel")), "tpl-1", "validated", user_id=UID)) is None


def test_set_package_status_refuses_an_oversized_report_before_touching_the_row():
    row = _row()
    big = {**OK, "padding": "x" * cp.MAX_VALIDATION_REPORT_BYTES}
    session = _session(row)
    with pytest.raises(ValueError, match="validation_report_too_large"):
        asyncio.run(cp.set_package_status(session, "tpl-1", "validated", user_id=UID, validation_report_json=big))
    assert row.status == "draft"
    session.commit.assert_not_awaited()


def test_set_package_status_rejects_a_report_whose_own_hash_matches_but_files_changed():
    """Freshness is keyed on the hash the store computes, never the report's own claim."""
    changed = {**CONFIG, "files": {**FILES, "manifest.ts": "export const manifest = 2;\n"}}
    row = _row(config=changed)
    with pytest.raises(ValueError, match="validation_report_stale"):
        asyncio.run(cp.set_package_status(_session(row), "tpl-1", "validated", user_id=UID, validation_report_json=OK))


def test_reapproving_is_idempotent_but_cannot_rewrite_the_report_or_preview():
    stamped = datetime(2026, 9, 1, tzinfo=timezone.utc)
    row = _row(status="approved", validation_report_json=OK, approved_at=stamped, preview_url="/p")
    with patch.object(cp, "_template_out", side_effect=lambda r: r):
        out = asyncio.run(cp.set_package_status(_session(row), "tpl-1", "approved", user_id=UID, validation_report_json=OK, preview_url="/p"))
    assert (out.status, out.approved_at) == ("approved", stamped)
    with pytest.raises(ValueError, match="package_immutable"):
        asyncio.run(cp.set_package_status(_session(row), "tpl-1", "approved", user_id=UID, validation_report_json={**OK, "x": 1}))
    with pytest.raises(ValueError, match="package_immutable"):
        asyncio.run(cp.set_package_status(_session(row), "tpl-1", "approved", user_id=UID, preview_url="/other"))


def test_set_package_status_omitting_preview_url_leaves_it_alone():
    # validated -> draft needs no fresh report, so this isolates the preview_url sentinel.
    row = _row(status="validated", preview_url="/p")
    with patch.object(cp, "_template_out", side_effect=lambda r: r):
        out = asyncio.run(cp.set_package_status(_session(row), "tpl-1", "draft", user_id=UID))
    assert out.preview_url == "/p"


def test_set_package_status_explicit_empty_string_clears_the_preview():
    row = _row(status="validated", preview_url="/p")
    with patch.object(cp, "_template_out", side_effect=lambda r: r):
        out = asyncio.run(cp.set_package_status(_session(row), "tpl-1", "draft", user_id=UID, preview_url=""))
    assert out.preview_url is None


def test_reapproving_cannot_clear_the_preview_either():
    """The explicit-clear sentinel still hits the same immutability wall as setting a new value."""
    stamped = datetime(2026, 9, 1, tzinfo=timezone.utc)
    row = _row(status="approved", validation_report_json=OK, approved_at=stamped, preview_url="/p")
    with pytest.raises(ValueError, match="package_immutable"):
        asyncio.run(cp.set_package_status(_session(row), "tpl-1", "approved", user_id=UID, preview_url=""))


# ── routes ──────────────────────────────────────────────────────────────────


async def _mock_session():
    yield MagicMock()


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_SECRET", SECRET)
    app = create_fastapi_app()
    app.dependency_overrides[get_session] = _mock_session
    with TestClient(app) as c:
        yield c


def _out(**overrides) -> RenderTemplateOut:
    now = datetime.now(timezone.utc)
    data = dict(id="tpl-1", user_id=UID, name="p", kind=cp.PACKAGE_KIND, source="agent_generated", status="validated", config=CONFIG, created_at=now, updated_at=now)
    data.update(overrides)
    return RenderTemplateOut(**data)


HEADERS = {"X-User-ID": UID, "X-Internal-Secret": SECRET}


def test_status_route_forwards_and_maps_conflicts(client):
    with patch("app.stores.clipstyle_packages.set_package_status", new=AsyncMock(return_value=_out(status="approved"))) as m:
        resp = client.patch("/v1/render-templates/tpl-1/status", headers=HEADERS, json={"status": "approved", "preview_url": "/v1/media/m/file"})
    assert resp.status_code == 200
    assert m.call_args.kwargs == {"user_id": UID, "validation_report_json": None, "preview_url": "/v1/media/m/file"}
    for detail in ("invalid_transition:approved->draft", "package_immutable", "package_version_already_approved",
                   "package_id_taken", "validation_report_stale", "validation_report_not_ok",
                   "validation_report_contract_version"):
        with patch("app.stores.clipstyle_packages.set_package_status", new=AsyncMock(side_effect=ValueError(detail))):
            resp = client.patch("/v1/render-templates/tpl-1/status", headers=HEADERS, json={"status": "draft"})
        assert (resp.status_code, resp.json()["detail"]) == (409, detail)
    with patch("app.stores.clipstyle_packages.set_package_status", new=AsyncMock(side_effect=ValueError("invalid_status"))):
        assert client.patch("/v1/render-templates/tpl-1/status", headers=HEADERS, json={"status": "published"}).status_code == 400
    with patch("app.stores.clipstyle_packages.set_package_status", new=AsyncMock(side_effect=ValueError("validation_report_too_large"))):
        assert client.patch("/v1/render-templates/tpl-1/status", headers=HEADERS, json={"status": "validated"}).status_code == 413
    with patch("app.stores.clipstyle_packages.set_package_status", new=AsyncMock(return_value=None)):
        assert client.patch("/v1/render-templates/tpl-1/status", headers=HEADERS, json={"status": "draft"}).status_code == 404


def test_status_route_keeps_the_auth_conventions(client):
    assert client.patch("/v1/render-templates/tpl-1/status", headers={"X-User-ID": UID}, json={"status": "draft"}).status_code == 401
    resp = client.patch("/v1/render-templates/tpl-1/status", headers={"X-Internal-Secret": SECRET}, json={"status": "draft"})
    assert (resp.status_code, resp.json()["detail"]) == (401, "user_id_required")


def test_editing_an_approved_package_is_a_conflict(client):
    with patch("app.stores.render_templates.update_template", new=AsyncMock(side_effect=ValueError("package_immutable"))):
        resp = client.put("/v1/render-templates/tpl-1", headers=HEADERS, json={"name": "p", "kind": cp.PACKAGE_KIND, "config": CONFIG})
    assert resp.status_code == 409
    with patch("app.stores.render_templates.update_template", new=AsyncMock(side_effect=ValueError("invalid_status"))):
        resp = client.put("/v1/render-templates/tpl-1", headers=HEADERS, json={"name": "p", "config": {}})
    assert resp.status_code == 400


def test_deleting_an_approved_package_is_a_conflict(client):
    with patch("app.stores.render_templates.archive_template", new=AsyncMock(side_effect=ValueError("package_immutable"))):
        resp = client.delete("/v1/render-templates/tpl-1", headers=HEADERS)
    assert (resp.status_code, resp.json()["detail"]) == (409, "package_immutable")
    with patch("app.stores.render_templates.archive_template", new=AsyncMock(return_value=True)):
        assert client.delete("/v1/render-templates/tpl-1", headers=HEADERS).status_code == 204
    with patch("app.stores.render_templates.archive_template", new=AsyncMock(return_value=False)):
        assert client.delete("/v1/render-templates/tpl-1", headers=HEADERS).status_code == 404


def test_internal_approved_listing_needs_only_the_secret(client):
    with patch("app.stores.clipstyle_packages.list_approved_packages", new=AsyncMock(return_value=[_out(status="approved")])):
        resp = client.get("/v1/internal/clipstyle-packages/approved", headers={"X-Internal-Secret": SECRET})
    assert resp.status_code == 200
    assert resp.json()[0]["status"] == "approved"
    assert client.get("/v1/internal/clipstyle-packages/approved").status_code == 401


# ── against a real (sqlite) table ───────────────────────────────────────────
# Dialect-independent behaviour: every rule below is enforced in Python, so it holds on sqlite and
# Postgres alike. Only the partial unique index is a DB guarantee (and sqlite gets it via sqlite_where).


@pytest.fixture()
def db():
    from sqlalchemy.dialects.postgresql import JSONB, UUID
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.ext.compiler import compiles

    from app.models import MediaItem, RenderTemplate

    @compiles(JSONB, "sqlite")
    def _jsonb_sqlite(element, compiler, **kw):  # pragma: no cover - trivial
        return "JSON"

    @compiles(UUID, "sqlite")
    def _uuid_sqlite(element, compiler, **kw):  # pragma: no cover - trivial
        return "VARCHAR(36)"

    engine = create_async_engine("sqlite+aiosqlite://")

    # MediaItem is included so the residual-fix tests (orphaned clipstyle preview media) can exercise
    # the real cross-table delete in the same transaction as a package edit/status change.
    tables = [RenderTemplate.__table__, MediaItem.__table__]
    # `'{}'::jsonb` is Postgres syntax: drop those server defaults for the sqlite DDL only (the ORM
    # `default=dict` still fills the columns), then restore them on the shared metadata.
    pg_defaults = {
        table: {c.name: c.server_default for c in table.columns if c.server_default is not None and "::" in str(c.server_default.arg)}
        for table in tables
    }

    async def setup():
        async with engine.begin() as conn:
            for table in tables:
                await conn.run_sync(lambda c, t=table: t.create(c))

    for table, defaults in pg_defaults.items():
        for name in defaults:
            table.c[name].server_default = None
    try:
        asyncio.run(setup())
    finally:
        for table, defaults in pg_defaults.items():
            for name, default in defaults.items():
                table.c[name].server_default = default
    yield async_sessionmaker(engine, expire_on_commit=False)
    asyncio.run(engine.dispose())


def _run(factory, fn):
    async def go():
        async with factory() as session:
            return await fn(session)

    return asyncio.run(go())


def _create(factory, *, user_id=UID, id="tpl-1", config=CONFIG, kind=cp.PACKAGE_KIND, **body):
    from app.stores import render_templates as rt

    return _run(factory, lambda s: rt.create_template(s, RenderTemplateIn(id=id, name="p", kind=kind, source="agent_generated", config=config, **body), user_id=user_id))


def _status(factory, target, *, id="tpl-1", user_id=UID, **kw):
    return _run(factory, lambda s: cp.set_package_status(s, id, target, user_id=user_id, **kw))


def test_lifecycle_against_a_real_table(db):
    from app.stores import render_templates as rt

    created = _create(db, status="active")
    assert (created.status, created.validation_report_json, created.approved_at) == ("draft", {}, None)
    assert _status(db, "validated", validation_report_json=OK).status == "validated"
    # editing a validated package drops it back to draft and clears the report
    edited = _run(db, lambda s: rt.update_template(s, "tpl-1", RenderTemplateIn(name="p2", kind=cp.PACKAGE_KIND, config=CONFIG), user_id=UID))
    assert (edited.status, edited.validation_report_json) == ("draft", {})
    with pytest.raises(ValueError, match="validation_report_not_ok"):
        _status(db, "validated")  # the cleared report cannot be reused
    _status(db, "validated", validation_report_json=OK)
    approved = _status(db, "approved", preview_url="/v1/media/m/file")
    assert (approved.status, approved.preview_url, approved.validation_report_json) == ("approved", "/v1/media/m/file", OK)
    assert approved.approved_at is not None
    with pytest.raises(ValueError, match="package_immutable"):
        _run(db, lambda s: rt.update_template(s, "tpl-1", RenderTemplateIn(name="p3", kind="carousel", config={}), user_id=UID))
    with pytest.raises(ValueError, match="invalid_transition:approved->draft"):
        _status(db, "draft", validation_report_json={"ok": False})
    # new version = new row: a clone starts in draft without the report or approval stamp
    clone = _run(db, lambda s: rt.clone_template(s, "tpl-1", RenderTemplateCloneIn(id="tpl-2"), user_id=UID))
    assert (clone.status, clone.validation_report_json, clone.approved_at, clone.config) == ("draft", {}, None, CONFIG)
    listed = _run(db, cp.list_approved_packages)
    assert [r.id for r in listed] == ["tpl-1"]


def test_other_users_cannot_see_or_move_a_package(db):
    _create(db)
    assert _status(db, "validated", user_id=OTHER_UID, validation_report_json=OK) is None
    from app.stores import render_templates as rt

    assert _run(db, lambda s: rt.update_template(s, "tpl-1", RenderTemplateIn(name="x", kind=cp.PACKAGE_KIND, config=CONFIG), user_id=OTHER_UID)) is None


def test_duplicate_approvals_are_refused_against_a_real_table(db):
    _create(db, id="a")
    _create(db, id="b")
    _create(db, id="c", user_id=OTHER_UID, config={**CONFIG, "manifest": {**CONFIG["manifest"], "version": 2}})
    for id in ("a", "b"):
        _status(db, "validated", id=id, validation_report_json=OK)
    _status(db, "validated", id="c", user_id=OTHER_UID, validation_report_json=OK)
    _status(db, "approved", id="a")
    with pytest.raises(ValueError, match="package_version_already_approved"):
        _status(db, "approved", id="b")
    with pytest.raises(ValueError, match="package_id_taken"):
        _status(db, "approved", id="c", user_id=OTHER_UID)
    assert [r.id for r in _run(db, cp.list_approved_packages)] == ["a"]


def test_the_unique_index_backs_the_python_check_when_it_is_bypassed(db):
    """A concurrent approval that slips past `_ensure_version_unique` hits the partial unique index."""
    _create(db, id="a")
    _create(db, id="b")
    for id in ("a", "b"):
        _status(db, "validated", id=id, validation_report_json=OK)
    _status(db, "approved", id="a")
    with patch.object(cp, "_ensure_version_unique", new=AsyncMock()):
        with pytest.raises(ValueError, match="package_version_already_approved"):
            _status(db, "approved", id="b")
    from app.models import RenderTemplate

    row = _run(db, lambda s: s.get(RenderTemplate, "b"))
    assert row.status == "validated"


def test_normal_templates_keep_their_behaviour(db):
    from app.stores import render_templates as rt

    created = _create(db, id="n", kind="carousel", config={"layout": "split"})
    assert (created.status, created.validation_report_json, created.approved_at) == ("active", {}, None)
    updated = _run(db, lambda s: rt.update_template(s, "n", RenderTemplateIn(name="n2", kind="carousel", status="archived", config={"layout": "x"}), user_id=UID))
    assert (updated.status, updated.config) == ("archived", {"layout": "x"})
    with pytest.raises(ValueError, match="invalid_status"):
        _run(db, lambda s: rt.update_template(s, "n", RenderTemplateIn(name="n2", kind="carousel", status="approved"), user_id=UID))
    with pytest.raises(ValueError, match="invalid_status"):
        _run(db, lambda s: rt.update_template(s, "n", RenderTemplateIn(name="n2", kind="carousel", status="validated"), user_id=UID))
    clone = _run(db, lambda s: rt.clone_template(s, "n", RenderTemplateCloneIn(id="n-copy"), user_id=UID))
    assert (clone.status, clone.kind) == ("active", "carousel")
    # the package status endpoint does not see ordinary templates
    assert _status(db, "draft", id="n") is None
    assert _run(db, lambda s: rt.archive_template(s, "n", user_id=UID)) is True


def test_approved_packages_cannot_be_archived_but_other_package_states_can(db):
    from app.stores import render_templates as rt

    for id in ("d", "v", "r", "a"):
        _create(db, id=id, config={**CONFIG, "manifest": {**CONFIG["manifest"], "version": {"a": 1}.get(id, 2)}})
    _status(db, "validated", id="v", validation_report_json={**OK, "package": {"sha256": cp.package_sha256(FILES)}})
    _status(db, "rejected", id="r")
    _status(db, "validated", id="a", validation_report_json=OK)
    _status(db, "approved", id="a")
    with pytest.raises(ValueError, match="package_immutable"):
        _run(db, lambda s: rt.archive_template(s, "a", user_id=UID))
    assert [r.id for r in _run(db, cp.list_approved_packages)] == ["a"]
    for id in ("d", "v", "r"):
        assert _run(db, lambda s: rt.archive_template(s, id, user_id=UID)) is True
    # another user's approved package stays hidden (404), not a conflict
    assert _run(db, lambda s: rt.archive_template(s, "a", user_id=OTHER_UID)) is False


def test_the_owner_of_an_id_can_approve_new_versions(db):
    _create(db, id="v1")
    _create(db, id="v2", config={**CONFIG, "manifest": {**CONFIG["manifest"], "version": 2}})
    for id in ("v1", "v2"):
        _status(db, "validated", id=id, validation_report_json=OK)
        assert _status(db, "approved", id=id).status == "approved"
    assert [r.id for r in _run(db, cp.list_approved_packages)] == ["v1", "v2"]


# ── fix round 1 ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("final", ["rejected", "draft"])
def test_an_archived_package_stays_dead(db, final):
    """DELETE of a rejected (or draft) package is terminal: no PUT, no status change. New work = clone."""
    from app.stores import render_templates as rt

    _create(db)
    if final == "rejected":
        _status(db, "rejected")
    assert _run(db, lambda s: rt.archive_template(s, "tpl-1", user_id=UID)) is True
    with pytest.raises(ValueError, match="package_immutable"):
        _run(db, lambda s: rt.update_template(s, "tpl-1", RenderTemplateIn(name="p", kind=cp.PACKAGE_KIND, config=CONFIG), user_id=UID))
    with pytest.raises(ValueError, match="package_immutable"):
        _run(db, lambda s: rt.update_template(s, "tpl-1", RenderTemplateIn(name="p", kind="carousel", config={}), user_id=UID))
    for target in ("draft", "validated", "rejected"):
        with pytest.raises(ValueError, match=f"invalid_transition:archived->{target}"):
            _status(db, target, validation_report_json=OK)
    from app.models import RenderTemplate

    assert _run(db, lambda s: s.get(RenderTemplate, "tpl-1")).status == "archived"
    clone = _run(db, lambda s: rt.clone_template(s, "tpl-1", RenderTemplateCloneIn(id="tpl-2"), user_id=UID))
    assert clone.status == "draft"


def test_archived_normal_templates_can_still_be_restored_by_put(db):
    from app.stores import render_templates as rt

    _create(db, id="n", kind="carousel", config={"layout": "split"})
    assert _run(db, lambda s: rt.archive_template(s, "n", user_id=UID)) is True
    restored = _run(db, lambda s: rt.update_template(s, "n", RenderTemplateIn(name="n", kind="carousel", status="active"), user_id=UID))
    assert restored.status == "active"


def test_kind_change_away_from_a_package_only_from_draft_and_it_drops_the_report(db):
    from app.stores import render_templates as rt

    _create(db, id="v")
    _status(db, "validated", id="v", validation_report_json=OK)
    with pytest.raises(ValueError, match="package_kind_change_requires_draft"):
        _run(db, lambda s: rt.update_template(s, "v", RenderTemplateIn(name="p", kind="carousel", config={}), user_id=UID))
    _create(db, id="d")
    _status(db, "draft", id="d", validation_report_json={"ok": False, "contract_version": 1})
    out = _run(db, lambda s: rt.update_template(s, "d", RenderTemplateIn(name="p", kind="carousel", config={}), user_id=UID))
    assert (out.kind, out.status, out.validation_report_json, out.approved_at) == ("carousel", "active", {}, None)


def test_kind_change_conflict_maps_to_409(client):
    with patch("app.stores.render_templates.update_template", new=AsyncMock(side_effect=ValueError("package_kind_change_requires_draft"))):
        resp = client.put("/v1/render-templates/tpl-1", headers=HEADERS, json={"name": "p", "config": {}})
    assert resp.status_code == 409


def test_cloning_a_package_drops_the_preview_but_normal_clones_keep_it(db):
    from app.stores import render_templates as rt

    _create(db, id="p", preview_url="/v1/media/old/file")
    _create(db, id="n", kind="carousel", config={}, preview_url="/v1/media/n/file")
    assert _run(db, lambda s: rt.clone_template(s, "p", RenderTemplateCloneIn(id="p2"), user_id=UID)).preview_url is None
    assert _run(db, lambda s: rt.clone_template(s, "n", RenderTemplateCloneIn(id="n2"), user_id=UID)).preview_url == "/v1/media/n/file"


def test_user_lists_hide_packages_unless_asked_and_never_carry_reports(db):
    from app.stores import render_templates as rt

    _create(db, id="p")
    _status(db, "validated", id="p", validation_report_json=OK)
    _create(db, id="n", kind="carousel", config={})
    assert [r.id for r in _run(db, lambda s: rt.list_templates(s, user_id=UID))] == ["n"]
    packages = _run(db, lambda s: rt.list_templates(s, user_id=UID, kind=cp.PACKAGE_KIND))
    assert [r.id for r in packages] == ["p"]
    assert "validation_report_json" not in packages[0].model_dump()
    # single GET and the internal approved list keep the report (the bridge reads registry_payload)
    assert _run(db, lambda s: rt.get_template(s, "p", user_id=UID)).validation_report_json == OK


def test_list_route_omits_reports(client):
    with patch("app.stores.render_templates.list_templates", new=AsyncMock(return_value=[_out(validation_report_json=OK)])):
        resp = client.get("/v1/render-templates?kind=clipstyle_package", headers=HEADERS)
    assert resp.status_code == 200
    assert "validation_report_json" not in resp.json()[0]
    with patch("app.stores.render_templates.get_template", new=AsyncMock(return_value=_out(validation_report_json=OK))):
        assert client.get("/v1/render-templates/tpl-1", headers=HEADERS).json()["validation_report_json"] == OK
    with patch("app.stores.clipstyle_packages.list_approved_packages", new=AsyncMock(return_value=[_out(status="approved", validation_report_json=OK)])):
        assert client.get("/v1/internal/clipstyle-packages/approved", headers={"X-Internal-Secret": SECRET}).json()[0]["validation_report_json"] == OK


def test_downgrade_refuses_while_package_lifecycle_rows_exist():
    import os
    import subprocess
    import sys

    root = Path(__file__).resolve().parent.parent
    out = subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "0037:0036", "--sql"],
        cwd=root, env={**os.environ, "DATABASE_URL": "postgresql://offline/offline"},
        capture_output=True, text=True, check=True,
    ).stdout
    guard = out.index("RAISE EXCEPTION")
    assert "status IN ('validated','approved','rejected')" in out[:guard]
    assert guard < out.index("DROP COLUMN validation_report_json")
    assert "UPDATE render_templates SET status" not in out


# ── final fix wave: F-I3 (preview_url is bridge-owned) ──────────────────────


def test_package_create_ignores_a_client_supplied_preview_url(db):
    """Ruling F-I3: the store ignores a client-set preview_url on a package create/PUT; it is
    bridge-owned and only ever set through the status PATCH."""
    created = _create(db, preview_url="/v1/media/attacker/file")
    assert created.preview_url is None


def test_package_put_resets_a_bridge_set_preview_even_without_a_client_value(db):
    from app.stores import render_templates as rt

    _create(db)
    _status(db, "validated", validation_report_json=OK, preview_url="/v1/media/bridge/file")
    edited = _run(
        db,
        lambda s: rt.update_template(
            s, "tpl-1", RenderTemplateIn(name="p2", kind=cp.PACKAGE_KIND, config=CONFIG), user_id=UID
        ),
    )
    assert edited.preview_url is None


def test_normal_template_put_still_accepts_a_client_supplied_preview_url(db):
    """F-I3 only touches packages: this stays additive for the existing non-package flow."""
    from app.stores import render_templates as rt

    _create(db, id="n", kind="carousel", config={}, preview_url="/v1/media/old/file")
    edited = _run(
        db,
        lambda s: rt.update_template(
            s, "n", RenderTemplateIn(name="n2", kind="carousel", config={}, preview_url="/v1/media/new/file"), user_id=UID
        ),
    )
    assert edited.preview_url == "/v1/media/new/file"


# ── final fix wave: Task 15 deferred minor (colliding id -> 409, not 500) ───


def test_create_with_a_colliding_id_is_a_conflict_not_a_500(db):
    from app.stores import render_templates as rt

    _create(db, id="dup", kind="carousel", config={})
    with pytest.raises(ValueError, match="render_template_id_taken"):
        _run(
            db,
            lambda s: rt.create_template(
                s, RenderTemplateIn(id="dup", name="other", kind="carousel", config={}), user_id=UID
            ),
        )
    _create(db, id="dup-pkg")
    with pytest.raises(ValueError, match="render_template_id_taken"):
        _run(
            db,
            lambda s: rt.create_template(
                s, RenderTemplateIn(id="dup-pkg", name="other", kind=cp.PACKAGE_KIND, config=CONFIG), user_id=OTHER_UID
            ),
        )


def test_clone_into_a_colliding_id_is_a_conflict_not_a_500(db):
    from app.stores import render_templates as rt

    _create(db, id="src", kind="carousel", config={})
    _create(db, id="dup", kind="carousel", config={})
    with pytest.raises(ValueError, match="render_template_id_taken"):
        _run(db, lambda s: rt.clone_template(s, "src", RenderTemplateCloneIn(id="dup"), user_id=UID))
    # colliding with another user's row (own or another user's, archived or not - rereview note)
    _create(db, id="pkg-src")
    _create(db, id="pkg-dup", user_id=OTHER_UID, config={**CONFIG, "manifest": {**CONFIG["manifest"], "version": 9}})
    with pytest.raises(ValueError, match="render_template_id_taken"):
        _run(db, lambda s: rt.clone_template(s, "pkg-src", RenderTemplateCloneIn(id="pkg-dup"), user_id=UID))


# ── Residual fix (M3/I-1): orphaned clipstyle preview media ─────────────────
# Every validation of a package uploads up to ~61 preview PNGs as the user's media, recorded in the
# bridge-owned `preview_media` key of the stored report (modal_bridge/previews.py). Whenever the
# store resets or replaces that report, the media those ids point to must be deleted too, in the
# same transaction where possible, scoped to the same user and the bridge's own marker
# (metadata.kind == "clipstyle_preview", media.CLIPSTYLE_PREVIEW_KIND) — never an approved row's media.


def _report_with_previews(sheet_id, *preview_ids, ok=True):
    """A report carrying the bridge's `preview_media` shape (modal_bridge/previews.py), for an
    otherwise-valid report against CONFIG/FILES."""
    base = OK if ok else {"ok": False, "contract_version": 1}
    return {
        **base,
        "preview_media": {
            "contact_sheet": {"media_id": sheet_id, "url": f"/v1/media/{sheet_id}/file"},
            "previews": [
                {"fixture": "a", "slide": 0, "frame": 0, "media_id": pid, "url": f"/v1/media/{pid}/file"}
                for pid in preview_ids
            ],
        },
    }


def _seed_media(factory, rows):
    from app.models import MediaItem

    async def go(session):
        for r in rows:
            session.add(MediaItem(**r))
        await session.commit()

    _run(factory, go)


def _media_row(id_, *, user_id=UID, kind="clipstyle_preview"):
    return dict(id=id_, user_id=user_id, type="image", metadata_=({"kind": kind} if kind else {}))


def _media_present(factory, ids):
    from app.models import MediaItem

    async def go(session):
        return {mid for mid in ids if await session.get(MediaItem, mid) is not None}

    return _run(factory, go)


def test_package_body_edit_deletes_the_old_reports_preview_media(db):
    from app.stores import render_templates as rt

    _create(db)
    sheet, p1 = str(uuid.uuid4()), str(uuid.uuid4())
    _seed_media(db, [_media_row(sheet), _media_row(p1)])
    _status(db, "validated", validation_report_json=_report_with_previews(sheet, p1))
    assert _media_present(db, [sheet, p1]) == {sheet, p1}
    _run(db, lambda s: rt.update_template(s, "tpl-1", RenderTemplateIn(name="p2", kind=cp.PACKAGE_KIND, config=CONFIG), user_id=UID))
    assert _media_present(db, [sheet, p1]) == set()


def test_kind_change_away_from_a_package_also_deletes_its_preview_media(db):
    from app.stores import render_templates as rt

    _create(db, id="d")
    sheet = str(uuid.uuid4())
    _seed_media(db, [_media_row(sheet)])
    _status(db, "draft", id="d", validation_report_json=_report_with_previews(sheet, ok=False))
    assert _media_present(db, [sheet]) == {sheet}
    _run(db, lambda s: rt.update_template(s, "d", RenderTemplateIn(name="p", kind="carousel", config={}), user_id=UID))
    assert _media_present(db, [sheet]) == set()


def test_status_patch_deletes_the_replaced_reports_preview_media(db):
    """draft <-> validated re-validation loop: a newer report replaces an older one via the status
    PATCH, and the old report's preview media (not also claimed by the new one) is dropped."""
    _create(db)
    old_sheet, new_sheet = str(uuid.uuid4()), str(uuid.uuid4())
    _seed_media(db, [_media_row(old_sheet), _media_row(new_sheet)])
    _status(db, "validated", validation_report_json=_report_with_previews(old_sheet))
    assert _media_present(db, [old_sheet, new_sheet]) == {old_sheet, new_sheet}
    _status(db, "validated", validation_report_json=_report_with_previews(new_sheet))
    assert _media_present(db, [old_sheet, new_sheet]) == {new_sheet}


def test_approved_packages_preview_media_is_never_deleted(db):
    _create(db)
    sheet = str(uuid.uuid4())
    _seed_media(db, [_media_row(sheet)])
    _status(db, "validated", validation_report_json=_report_with_previews(sheet))
    _status(db, "approved", preview_url=f"/v1/media/{sheet}/file")
    # idempotent re-approval retry with the exact same report: a no-op, so no deletion either.
    _status(db, "approved", validation_report_json=_report_with_previews(sheet), preview_url=f"/v1/media/{sheet}/file")
    assert _media_present(db, [sheet]) == {sheet}
    # a refused attempt to change the approved row's report must not touch the media either.
    with pytest.raises(ValueError, match="package_immutable"):
        _status(db, "approved", validation_report_json=_report_with_previews(str(uuid.uuid4())))
    assert _media_present(db, [sheet]) == {sheet}


def test_only_the_same_users_marked_preview_media_is_deleted(db):
    """The old report's preview_media ids are only a lead: the store still scopes the delete to the
    caller's own user AND the bridge's own clipstyle_preview marker, so a report that (somehow)
    pointed at someone else's row or a non-preview row can never make this delete reach it."""
    from app.stores import render_templates as rt

    _create(db)
    mine, someone_elses, not_a_preview = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    _seed_media(
        db,
        [
            _media_row(mine),
            dict(id=someone_elses, user_id=OTHER_UID, type="image", metadata_={"kind": "clipstyle_preview"}),
            dict(id=not_a_preview, user_id=UID, type="image", metadata_={"source": "manual_upload"}),
        ],
    )
    _status(db, "validated", validation_report_json=_report_with_previews(mine, someone_elses, not_a_preview))
    _run(db, lambda s: rt.update_template(s, "tpl-1", RenderTemplateIn(name="p2", kind=cp.PACKAGE_KIND, config=CONFIG), user_id=UID))
    assert _media_present(db, [mine]) == set()
    assert _media_present(db, [someone_elses, not_a_preview]) == {someone_elses, not_a_preview}


def test_a_failed_delete_does_not_fail_the_edit(db):
    from app.stores import render_templates as rt

    _create(db)
    sheet = str(uuid.uuid4())
    _seed_media(db, [_media_row(sheet)])
    _status(db, "validated", validation_report_json=_report_with_previews(sheet))
    with patch("app.stores.clipstyle_packages.media_store.delete_clipstyle_preview_media", new=AsyncMock(side_effect=RuntimeError("boom"))):
        edited = _run(
            db,
            lambda s: rt.update_template(s, "tpl-1", RenderTemplateIn(name="p2", kind=cp.PACKAGE_KIND, config=CONFIG), user_id=UID),
        )
    assert (edited.status, edited.validation_report_json) == ("draft", {})
    assert _media_present(db, [sheet]) == {sheet}  # cleanup failed (logged), but the edit still landed
