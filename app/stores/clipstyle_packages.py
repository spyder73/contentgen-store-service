"""Lifecycle rules for render_templates rows of kind "clipstyle_package" (sub-project #2).

draft -> validated -> approved | rejected; approved and rejected rows are immutable (a new version
is a new row, via clone). Every rule here is enforced in Python so it holds on any dialect; the
partial unique index on id@version (migration 0037) is the database backstop for concurrent
approvals.

`package_sha256` mirrors remotion/src/validator/package-files.ts `packageSha256` (and
modal_bridge/validator_types.py): tests/fixtures/package-files-vectors.json pins all three.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import RenderTemplate
from ..schemas import RenderTemplateIn, RenderTemplateOut
from . import media as media_store

logger = logging.getLogger(__name__)

PACKAGE_KIND = "clipstyle_package"
PACKAGE_STATUSES = {"draft", "validated", "approved", "rejected"}
REPORT_CONTRACT_VERSION = 1
MAX_VALIDATION_REPORT_BYTES = 2 * 1024 * 1024
# Mirrors remotion/src/validator/package-files.ts `PACKAGE_PATH_PATTERN` and
# modal_bridge/validator_types.py `MAX_FILES`/`MAX_FILE_BYTES`/`MAX_TOTAL_BYTES`. Enforced here too
# (not only by the runner's check_files) so a bad path or an oversized package is refused at write
# time (400) instead of only after a full validation round trip (M2).
PACKAGE_PATH_PATTERN = re.compile(
    r"(?:manifest\.ts|schema\.ts|Template\.tsx|validate\.ts|writing-guide\.md"
    r"|fixtures/(?!constructor\.json\Z)[a-z0-9][a-z0-9-]{0,63}\.json"
    r"|components/[A-Z][A-Za-z0-9]{0,63}\.tsx|lib/[a-z][A-Za-z0-9-]{0,63}\.ts)"
)
MAX_FILES = 48
MAX_FILE_BYTES = 64 * 1024
MAX_TOTAL_BYTES = 512 * 1024
_ALLOWED = {
    ("draft", "draft"), ("draft", "validated"), ("draft", "rejected"),
    ("validated", "draft"), ("validated", "validated"), ("validated", "approved"), ("validated", "rejected"),
    ("approved", "approved"),
}
_MANIFEST_ID = re.compile(r"[a-z][A-Za-z0-9]{2,47}")
# Phase 4: the contract a style renders (design: "implements: <contract>@<version>").
_IMPLEMENTS = re.compile(r"[a-z][A-Za-z0-9]{2,47}@[1-9][0-9]{0,5}")


def _template_out(row: RenderTemplate) -> RenderTemplateOut:
    return RenderTemplateOut.model_validate(row)


def _utf8(text: str) -> bytes:
    """Node's UTF-8 encoding: surrogate pairs joined, lone surrogates become U+FFFD."""
    return text.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace").encode("utf-8")


def _js_sort_key(text: str) -> bytes:
    """JS `Array.prototype.sort` order: UTF-16 code units."""
    return text.encode("utf-16-be", "surrogatepass")


def package_sha256(files: dict[str, str]) -> str:
    digest = hashlib.sha256()
    for path in sorted(files, key=_js_sort_key):
        digest.update(_utf8(path) + b"\0" + _utf8(files[path]) + b"\0")
    return digest.hexdigest()


def check_package_config(config: dict) -> None:
    manifest = config.get("manifest") if isinstance(config, dict) else None
    if not isinstance(manifest, dict):
        raise ValueError("invalid_package_config:manifest")
    if not isinstance(manifest.get("id"), str) or not _MANIFEST_ID.fullmatch(manifest["id"]):
        raise ValueError("invalid_package_config:manifest.id")
    version = manifest.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ValueError("invalid_package_config:manifest.version")
    if manifest.get("kind") not in ("carousel", "video"):
        raise ValueError("invalid_package_config:manifest.kind")
    files = config.get("files")
    if not isinstance(files, dict) or not files or not all(isinstance(k, str) and isinstance(v, str) for k, v in files.items()):
        raise ValueError("invalid_package_config:files")
    if len(files) > MAX_FILES:
        raise ValueError("invalid_package_config:files.count")
    total = 0
    for path, content in files.items():
        if not PACKAGE_PATH_PATTERN.fullmatch(path):
            raise ValueError("invalid_package_config:files.path")
        size = len(_utf8(content))
        if size > MAX_FILE_BYTES:
            raise ValueError("invalid_package_config:files.size")
        total += size
    if total > MAX_TOTAL_BYTES:
        raise ValueError("invalid_package_config:files.size")
    if "implements" in config:
        implements = config["implements"]
        if not isinstance(implements, str) or not _IMPLEMENTS.fullmatch(implements) or implements.split("@", 1)[0] == manifest["id"]:
            raise ValueError("invalid_package_config:implements")


def _check_report_ok_and_fresh(report: dict | None, files: dict[str, str]) -> None:
    """ok + contract v1 + bound to the files as they are now. The report's own hash is only compared
    against the hash computed here, so a report can never vouch for files it was not run on."""
    if not isinstance(report, dict) or report.get("ok") is not True:
        raise ValueError("validation_report_not_ok")
    contract = report.get("contract_version")
    if type(contract) is not int or contract != REPORT_CONTRACT_VERSION:
        raise ValueError("validation_report_contract_version")
    package = report.get("package")
    if not isinstance(package, dict) or package.get("sha256") != package_sha256(files):
        raise ValueError("validation_report_stale")


def next_package_status(current: str, target: str, *, report: dict | None, files: dict[str, str]) -> str:
    if target not in PACKAGE_STATUSES:
        raise ValueError("invalid_status")
    if (current, target) not in _ALLOWED:
        raise ValueError(f"invalid_transition:{current}->{target}")
    if target in ("validated", "approved") and current != "approved":
        _check_report_ok_and_fresh(report, files)
    return target


def guard_package_update(row: RenderTemplate, *, new_kind: str | None = None) -> None:
    """Approved, rejected and archived packages are frozen (an archived package cannot be revived by
    PUT; clone it instead). Leaving the package kind is only allowed from draft."""
    if row.kind != PACKAGE_KIND:
        return
    if row.status in ("approved", "rejected", "archived"):
        raise ValueError("package_immutable")
    if new_kind is not None and new_kind != PACKAGE_KIND and row.status != "draft":
        raise ValueError("package_kind_change_requires_draft")


def guard_package_archive(row: RenderTemplate) -> None:
    """Approved versions cannot be archived: retiring one will be an explicit, later feature."""
    if row.kind == PACKAGE_KIND and row.status == "approved":
        raise ValueError("package_immutable")


# Media ids in a bridge-owned report (modal_bridge/previews.py media ids are UUIDs; this is the same
# lenient id-shape check the bridge itself uses before ever sending an id to a delete).
_MEDIA_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,63}")


def _bridge_media_ids(report: dict | None) -> list[str]:
    """Media ids the bridge wrote into a report: the `preview_media` key (contact sheet +
    previews, modal_bridge/previews.py `media_ids`) and, from Phase 4, the `render_bundle` key
    (modal_bridge/bundles.py). Any other shape yields nothing for that key; one malformed id makes
    the whole preview list unusable, as before."""
    if not isinstance(report, dict):
        return []
    ids: list[str] = []
    preview_media = report.get("preview_media")
    if isinstance(preview_media, dict):
        sheet, previews = preview_media.get("contact_sheet"), preview_media.get("previews")
        entries = ([sheet] if sheet is not None else []) + (previews if isinstance(previews, list) else [])
        found = [e.get("media_id") if isinstance(e, dict) else None for e in entries]
        if all(isinstance(i, str) and _MEDIA_ID.fullmatch(i) for i in found):
            ids.extend(found)
    bundle = report.get("render_bundle")
    bundle_id = bundle.get("media_id") if isinstance(bundle, dict) else None
    if isinstance(bundle_id, str) and _MEDIA_ID.fullmatch(bundle_id):
        ids.append(bundle_id)
    return ids


async def drop_stale_preview_media(
    session: AsyncSession, template_id: str, user_id: str, old_report: dict | None, new_report: dict | None
) -> frozenset[str]:
    """Deletes the OLD report's preview media that the NEW report doesn't also carry, in the same
    transaction as whatever reset or replaced the report (a package body edit, a kind change away
    from a package, or a status PATCH that stores a newer report over an older one).

    Called only where the report is actually changing on a non-approved row (an approved package's
    report can never be reset or replaced — see `guard_package_update` and the immutability check in
    `set_package_status`), so this never reaches an approved package's media.

    The delete runs inside a savepoint: a failure there is isolated and only logged, never allowed
    to fail the edit that orphaned the media (final fix wave M3/I-1 residual). Returns the ids whose
    delete failed (empty when there was nothing to delete, or the delete succeeded), so a caller
    that must not lose them — archive's X-M4 orphan sweep — can keep them instead of losing them
    for good.
    """
    old_ids = set(_bridge_media_ids(old_report)) - set(_bridge_media_ids(new_report))
    if not old_ids:
        return frozenset()
    try:
        async with session.begin_nested():
            await media_store.delete_clipstyle_preview_media(session, user_id, old_ids)
    except Exception:
        logger.warning(
            "clipstyle package %s: could not delete %d orphaned preview media for user %s",
            template_id, len(old_ids), user_id, exc_info=True,
        )
        return frozenset(old_ids)
    return frozenset()


def apply_package_body(row: RenderTemplate, body: RenderTemplateIn) -> None:
    """Any create/edit of a package lands in draft with no report: a report never outlives its files.

    `preview_url` is bridge-owned (set only through the status PATCH, see `set_package_status`): a
    client-supplied value on create/PUT is always ignored, never stored, and any edit resets the
    field to None even if the body didn't touch it, since the old preview described the pre-edit
    files (ruling F-I3)."""
    check_package_config(body.config)
    if body.status in ("validated", "approved", "rejected"):
        raise ValueError("package_status_via_status_endpoint")
    # Built-in packages are code-owned (#1's committed contracts); a stored row claiming "builtin"
    # would be registered with render_backend=local. The Go backend forwards `source` verbatim.
    if body.source == "builtin":
        raise ValueError("invalid_package_config:source")
    row.status = "draft"
    row.validation_report_json = {}
    row.approved_at = None
    row.preview_url = None


def _check_report_size(report: dict) -> None:
    size = len(json.dumps(report, ensure_ascii=False, separators=(",", ":")).encode("utf-8", "surrogatepass"))
    if size > MAX_VALIDATION_REPORT_BYTES:
        raise ValueError("validation_report_too_large")


async def _ensure_version_unique(session: AsyncSession, row: RenderTemplate) -> None:
    manifest = row.config["manifest"]
    stmt = select(RenderTemplate).where(
        RenderTemplate.kind == PACKAGE_KIND, RenderTemplate.status == "approved", RenderTemplate.id != row.id
    )
    for other in (await session.execute(stmt)).scalars():
        other_manifest = (other.config or {}).get("manifest") or {}
        if other_manifest.get("id") == manifest["id"] and other.user_id != row.user_id:
            # A manifest.id belongs to the user who first got it approved (the registry is keyed by
            # style id). Check-then-act: two users' simultaneous FIRST approvals of one id are not
            # caught (no DB constraint can express it) - accepted, approvals are human-gated.
            raise ValueError("package_id_taken")
        if (other_manifest.get("id"), other_manifest.get("version")) == (manifest["id"], manifest["version"]):
            raise ValueError("package_version_already_approved")


async def _inherits_revoked(session: AsyncSession, row: RenderTemplate) -> bool:
    """X-M3: a revoke can be routed around without "New version" by just resubmitting a fresh
    draft of a higher version — M15 already covers the clone path, which copies `source.visibility`
    before approval, but a plain new draft starts `private` regardless. So a fresh approval must
    also inherit `revoked` when any other approved version of this same (owner, manifest.id) was
    revoked, closing the gap the same way."""
    manifest_id = row.config["manifest"]["id"]
    stmt = select(RenderTemplate).where(
        RenderTemplate.kind == PACKAGE_KIND,
        RenderTemplate.status == "approved",
        RenderTemplate.user_id == row.user_id,
        RenderTemplate.visibility == "revoked",
        RenderTemplate.id != row.id,
    )
    for other in (await session.execute(stmt)).scalars():
        other_manifest = (other.config or {}).get("manifest") or {}
        if other_manifest.get("id") == manifest_id:
            return True
    return False


async def set_package_status(
    session: AsyncSession,
    id: str,
    target: str,
    *,
    user_id: str,
    validation_report_json: dict | None = None,
    preview_url: str | None = None,
) -> RenderTemplateOut | None:
    """`preview_url` is bridge-owned end to end: this is the only place a package's preview can be
    set or cleared (ruling F-I3). `None` (omitted) leaves it alone; `""` is an explicit clear; any
    other string is the new value. Either way it is still refused on an approved row unless it's a
    no-op retry, same as the report."""
    if validation_report_json is not None:
        _check_report_size(validation_report_json)
    row = await session.get(RenderTemplate, id)
    if row is None or row.user_id != user_id or row.kind != PACKAGE_KIND:
        return None
    report = validation_report_json if validation_report_json is not None else (row.validation_report_json or {})
    new_status = next_package_status(row.status, target, report=report, files=(row.config or {}).get("files") or {})
    new_preview_url = None if preview_url == "" else preview_url
    if row.status == "approved":
        # Idempotent re-approval (e.g. a retried bridge call) may not rewrite what was approved.
        if (validation_report_json is not None and validation_report_json != row.validation_report_json) or (
            preview_url is not None and new_preview_url != row.preview_url
        ):
            raise ValueError("package_immutable")
        return _template_out(row)
    old_report = row.validation_report_json
    if new_status == "approved":
        await _ensure_version_unique(session, row)
        row.approved_at = datetime.now(timezone.utc)
        if await _inherits_revoked(session, row):
            row.visibility = "revoked"
    row.status = new_status
    if validation_report_json is not None:
        row.validation_report_json = validation_report_json
        # A newer report replaces the old one here: drop the old one's preview media now that
        # nothing (row.status == "approved" already returned above) references it any more.
        await drop_stale_preview_media(session, id, user_id, old_report, validation_report_json)
    if preview_url is not None:
        row.preview_url = new_preview_url
    try:
        await session.commit()
    except IntegrityError as exc:  # concurrent approval of the same id@version (partial unique index)
        await session.rollback()
        raise ValueError("package_version_already_approved") from exc
    await session.refresh(row)
    return _template_out(row)


async def list_approved_packages(session: AsyncSession) -> list[RenderTemplateOut]:
    stmt = select(RenderTemplate).where(RenderTemplate.kind == PACKAGE_KIND, RenderTemplate.status == "approved")
    return [_template_out(row) for row in (await session.execute(stmt.order_by(RenderTemplate.approved_at, RenderTemplate.id))).scalars()]
