"""Playbooks: a user's edits of the Markdown how-tos the chat agent follows (migration 0046).

Shipped playbooks are files in the backend; a row here is a user's own copy or edit, with an
append-only version history. ``expect_version`` is the only concurrency control: a save names
the version the caller read, and the one UPDATE that bumps it refuses a stale caller. The same
migration lets an approval be ``playbook.save``, the card the agent's edits wait behind.

The behaviour tests use the real-app harness of ``test_chat.py`` (a sqlite database built from
the models) with foreign keys switched on, so ``ON DELETE CASCADE`` is exercised for real. The
migration's DDL is asserted by rendering it offline in the postgres dialect.
"""
from __future__ import annotations

import asyncio
import importlib.util
import re
from pathlib import Path
from typing import get_args

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import UniqueConstraint, delete, event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db import get_session
from app.fastapi_app import create_fastapi_app
from app.models import ChatApproval, Playbook, PlaybookVersion, User
from app.schemas import ChatAction
from tests.test_chat import SECRET, USER_A, USER_B, Env, _consume_body, _create_tables, _h, _render

MIGRATION_PATH = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0046_playbooks.py"

ACTIONS_0045 = (
    "'run.start', 'run.continue', 'image.generate', 'clip.render', 'plan.approve', 'frames.approve', "
    "'graph.extend', 'sourcing.run', 'production.budget'"
)
ACTIONS_0046 = f"{ACTIONS_0045}, 'playbook.save'"

SAVE = {
    "name": "Carousel",
    "description": "How to make a swipe post",
    "body": "# Carousel\n\n1. Find the story.\n",
    "note": "first draft",
    "author": "user",
    "expect_version": 0,
}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("INTERNAL_API_SECRET", SECRET)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'playbooks.db'}", poolclass=NullPool)

    @event.listens_for(engine.sync_engine, "connect")
    def _enable_foreign_keys(dbapi_conn, _record):  # pragma: no cover - trivial
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async def create() -> None:
        await _create_tables(engine)
        async with engine.begin() as conn:
            for table in (Playbook.__table__, PlaybookVersion.__table__):
                await conn.run_sync(lambda c, t=table: t.create(c))

    asyncio.run(create())
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _session():
        async with factory() as session:
            yield session

    app = create_fastapi_app()
    app.dependency_overrides[get_session] = _session
    with TestClient(app) as client:
        yield Env(client, factory)
    asyncio.run(engine.dispose())


def _url(slug: str = "carousel", user: str = USER_A, tail: str = "") -> str:
    return f"/v1/users/{user}/playbooks/{slug}{tail}"


def _save(env: Env, slug: str = "carousel", user: str = USER_A, **over):
    return env.client.put(_url(slug, user), json={**SAVE, **over}, headers=_h(user))


def _saved(env: Env, slug: str = "carousel", user: str = USER_A, **over) -> dict:
    r = _save(env, slug, user, **over)
    assert r.status_code == 200, r.text
    return r.json()


def _versions(env: Env, slug: str = "carousel", user: str = USER_A) -> list[dict]:
    r = env.client.get(_url(slug, user, "/versions"), headers=_h(user))
    assert r.status_code == 200, r.text
    return r.json()


# ── save ─────────────────────────────────────────────────────────────────────


def test_first_save_is_version_1(env):
    out = _saved(env, based_on_shipped_version=3)
    assert (out["slug"], out["user_id"], out["version"], out["status"]) == ("carousel", USER_A, 1, "active")
    assert (out["name"], out["description"], out["body"]) == (SAVE["name"], SAVE["description"], SAVE["body"])
    assert out["based_on_shipped_version"] == 3
    assert set(out) == {
        "id", "user_id", "slug", "name", "description", "body", "version", "based_on_shipped_version",
        "status", "created_at", "updated_at",
    }
    assert env.client.get(_url(), headers=_h()).json() == out
    assert env.client.get(f"/v1/users/{USER_A}/playbooks", headers=_h()).json() == [out]
    [first] = _versions(env)
    assert (first["version"], first["note"], first["author"]) == (1, "first draft", "user")


def test_a_second_save_is_version_2_and_keeps_what_it_does_not_name(env):
    first = _saved(env, based_on_shipped_version=3)
    second = _saved(env, expect_version=1, body="# Carousel v2\n", note="tighter", author="agent")
    assert (second["id"], second["version"], second["body"]) == (first["id"], 2, "# Carousel v2\n")
    assert second["created_at"] == first["created_at"]
    # The shipped version a copy came from stays unless a save names another.
    assert second["based_on_shipped_version"] == 3
    assert _saved(env, expect_version=2, based_on_shipped_version=5)["based_on_shipped_version"] == 5


def test_stale_save_is_refused_and_changes_nothing(env):
    _saved(env)
    _saved(env, expect_version=1, body="v2 body", note="two")
    before = env.client.get(_url(), headers=_h()).json()
    stale = _save(env, expect_version=1, body="lost update", note="stale", name="Renamed")
    assert (stale.status_code, stale.json()["detail"]) == (409, "stale_playbook_version")
    assert env.client.get(_url(), headers=_h()).json() == before
    assert [v["version"] for v in _versions(env)] == [2, 1]
    assert env.count(PlaybookVersion) == 2


def test_a_first_save_over_an_existing_playbook_is_stale(env):
    _saved(env)
    again = _save(env, body="clobber")
    assert (again.status_code, again.json()["detail"]) == (409, "stale_playbook_version")
    assert env.client.get(_url(), headers=_h()).json()["body"] == SAVE["body"]
    assert env.count(PlaybookVersion) == 1


def test_a_save_that_expects_a_version_of_a_missing_playbook_is_stale(env):
    r = _save(env, expect_version=2)
    assert (r.status_code, r.json()["detail"]) == (409, "stale_playbook_version")
    assert env.count(Playbook) == 0 and env.count(PlaybookVersion) == 0


def test_every_save_appends_a_version(env):
    _saved(env, body="one", note="n1")
    _saved(env, expect_version=1, body="two", note="n2", author="agent")
    _saved(env, expect_version=2, body="three", note="", author="user")
    listed = _versions(env)
    assert [(v["version"], v["note"], v["author"]) for v in listed] == [
        (3, "", "user"), (2, "n2", "agent"), (1, "n1", "user"),
    ]
    assert all("body" not in v for v in listed)
    assert all(set(v) == {"id", "playbook_id", "version", "note", "author", "created_at"} for v in listed)
    one = env.client.get(_url(tail="/versions/2"), headers=_h()).json()
    assert (one["version"], one["body"], one["note"], one["author"]) == (2, "two", "n2", "agent")
    assert one["id"] == listed[1]["id"] and one["playbook_id"] == listed[1]["playbook_id"]
    # The playbook row itself is the newest version.
    assert env.client.get(_url(), headers=_h()).json()["body"] == "three"


def test_an_unknown_version_or_playbook_is_a_404(env):
    _saved(env)
    assert env.client.get(_url(tail="/versions/2"), headers=_h()).status_code == 404
    assert env.client.get(_url(tail="/versions/0"), headers=_h()).status_code == 404
    assert env.client.get(_url("nope"), headers=_h()).status_code == 404
    assert env.client.get(_url("nope", tail="/versions"), headers=_h()).status_code == 404
    assert env.client.get(_url("nope", tail="/versions/1"), headers=_h()).status_code == 404


def test_body_over_32000_is_refused(env):
    too_long = _save(env, body="x" * 32001)
    assert too_long.status_code == 422
    assert env.count(Playbook) == 0 and env.count(PlaybookVersion) == 0
    ok = _save(env, body="x" * 32000)
    assert ok.status_code == 200 and len(ok.json()["body"]) == 32000
    over = _save(env, expect_version=1, body="y" * 32001)
    assert over.status_code == 422
    assert env.client.get(_url(), headers=_h()).json()["version"] == 1


def test_a_save_needs_a_name_an_author_and_a_version_to_expect(env):
    for field in ("name", "author", "expect_version", "body"):
        body = {k: v for k, v in SAVE.items() if k != field}
        r = env.client.put(_url(), json=body, headers=_h())
        assert r.status_code == 422, field
    assert _save(env, author="system").status_code == 422
    assert _save(env, name="").status_code == 422
    assert _save(env, expect_version=-1).status_code == 422
    assert env.count(Playbook) == 0


def test_a_slug_is_lowercase_words(env):
    for slug in ("Carousel", "a b", "-x", "x" * 65, "a.b"):
        r = _save(env, slug)
        assert (r.status_code, r.json()["detail"]) == (422, "invalid_slug"), slug
    assert _save(env, "technology_internet-reacts2").status_code == 200
    # A read of an odd slug is just a not-found.
    assert env.client.get(_url("Not A Slug"), headers=_h()).status_code == 404


# ── users ────────────────────────────────────────────────────────────────────


def test_slugs_are_per_user(env):
    mine = _saved(env, user=USER_A, body="mine")
    theirs = _saved(env, user=USER_B, body="theirs")
    assert mine["id"] != theirs["id"] and (mine["version"], theirs["version"]) == (1, 1)
    assert env.client.get(_url(user=USER_A), headers=_h(USER_A)).json()["body"] == "mine"
    assert env.client.get(_url(user=USER_B), headers=_h(USER_B)).json()["body"] == "theirs"
    # B's edit is B's own history, and A's playbook did not move.
    _saved(env, user=USER_B, expect_version=1, body="theirs 2")
    assert env.client.get(_url(user=USER_A), headers=_h(USER_A)).json()["version"] == 1
    assert [v["version"] for v in _versions(env, user=USER_A)] == [1]
    assert [v["version"] for v in _versions(env, user=USER_B)] == [2, 1]
    assert [p["body"] for p in env.client.get(f"/v1/users/{USER_A}/playbooks", headers=_h(USER_A)).json()] == ["mine"]


def test_the_path_user_must_be_the_caller(env):
    _saved(env, user=USER_A)
    theirs = _h(USER_B)
    for tail in ("", "/versions", "/versions/1"):
        assert env.client.get(_url(tail=tail), headers=theirs).status_code == 403, tail
    assert env.client.get(f"/v1/users/{USER_A}/playbooks", headers=theirs).status_code == 403
    assert env.client.put(_url(), json={**SAVE, "expect_version": 1}, headers=theirs).status_code == 403
    no_user = {"X-Internal-Secret": SECRET}
    assert env.client.get(f"/v1/users/{USER_A}/playbooks", headers=no_user).status_code == 401
    assert env.client.get(_url(), headers=_h()).json()["version"] == 1


def test_list_is_that_users_rows_by_slug(env):
    _saved(env, "zeta", name="Zeta")
    _saved(env, "alpha", name="Alpha")
    _saved(env, "other", user=USER_B)
    listed = env.client.get(f"/v1/users/{USER_A}/playbooks", headers=_h()).json()
    assert [p["slug"] for p in listed] == ["alpha", "zeta"]
    assert env.client.get(f"/v1/users/{USER_B}/playbooks", headers=_h(USER_B)).json()[0]["slug"] == "other"


def test_user_delete_cascades(env):
    _saved(env, user=USER_A)
    _saved(env, user=USER_A, expect_version=1)
    _saved(env, user=USER_B)

    async def drop(session):
        await session.execute(delete(User).where(User.id == USER_A))
        await session.commit()

    env.db(drop)
    assert env.count(Playbook, Playbook.user_id == USER_A) == 0
    assert env.count(PlaybookVersion) == 1  # only B's first version is left
    assert env.client.get(_url(user=USER_B), headers=_h(USER_B)).json()["version"] == 1


# ── the playbook.save approval ───────────────────────────────────────────────

SUBJECT = "playbook:carousel|v2|body:" + "ab" * 32


def test_playbook_save_is_an_approval_action(env):
    thread_id, approval_id, token = env.approved(action="playbook.save", subject=SUBJECT, amount_usd=0)
    stored = env.client.get(f"/v1/chat/approvals/{approval_id}", headers=_h()).json()
    assert (stored["action"], stored["status"], stored["subject"]) == ("playbook.save", "approved", SUBJECT)
    r = env.consume(_consume_body(thread_id, token, action="playbook.save", subject=SUBJECT, needed_usd=0))
    assert r.status_code == 200 and r.json()["status"] == "used"
    assert env.count(ChatApproval, ChatApproval.action == "playbook.save") == 1
    bad = env.client.post(
        f"/v1/chat/threads/{thread_id}/approvals", json={"action": "playbook.edit", "subject": "s"}, headers=_h()
    )
    assert bad.status_code == 422


# ── migration ────────────────────────────────────────────────────────────────


def _load_migration_module():
    spec = importlib.util.spec_from_file_location("migration_0046_playbooks", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_0046_chains_onto_0045():
    module = _load_migration_module()
    assert module.revision == "0046" and module.down_revision == "0045"


def test_migration_0046_renders_the_tables_and_the_action_constraint():
    sql = _render(_load_migration_module().upgrade)
    assert "CREATE TABLE playbooks" in sql and "CREATE TABLE playbook_versions" in sql
    assert sql.index("CREATE TABLE playbooks") < sql.index("CREATE TABLE playbook_versions")
    # The owner owns the playbooks, and the playbook owns its versions: both cascade.
    assert "FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE" in sql
    assert "FOREIGN KEY(playbook_id) REFERENCES playbooks (id) ON DELETE CASCADE" in sql
    assert "CONSTRAINT uq_playbooks_user_slug UNIQUE (user_id, slug)" in sql
    assert "CONSTRAINT uq_playbook_versions_playbook_version UNIQUE (playbook_id, version)" in sql
    assert "CHECK (status IN ('active', 'archived'))" in sql
    assert "CHECK (author IN ('user', 'agent'))" in sql
    name = "ck_chat_approvals_action"
    assert f"ALTER TABLE chat_approvals DROP CONSTRAINT {name}" in sql
    assert f"ALTER TABLE chat_approvals ADD CONSTRAINT {name} CHECK (action IN ({ACTIONS_0046}))" in sql
    assert sql.count("DROP CONSTRAINT") == 1 and sql.count("ADD CONSTRAINT") == 1


def test_migration_0046_downgrade_refuses_over_new_approvals_then_drops_the_tables():
    sql = _render(_load_migration_module().downgrade)
    assert sql.count("RAISE EXCEPTION") == 1
    assert "action = 'playbook.save'" in sql
    assert "downgrade 0046 refused: chat has playbook.save approvals; delete them first" in sql
    assert f"CHECK (action IN ({ACTIONS_0045}))" in sql
    assert sql.index("DROP TABLE playbook_versions") < sql.index("DROP TABLE playbooks")
    assert sql.index("RAISE EXCEPTION") < sql.index("DROP CONSTRAINT") < sql.index("DROP TABLE playbook_versions")


def test_the_models_declare_what_the_migration_makes():
    """Model/migration drift means a future autogenerate "fixes" production. (The approval
    action list is pinned here, as 0045 took the pin over from 0044.)"""
    sql = _render(_load_migration_module().upgrade)
    modelled = {
        (t.name, c.name): re.sub(r"\s+", " ", str(c.sqltext))
        for t in (Playbook.__table__, PlaybookVersion.__table__, ChatApproval.__table__)
        for c in t.constraints
        if getattr(c, "sqltext", None) is not None and c.name
    }
    for table, name in (
        ("playbooks", "ck_playbooks_status"),
        ("playbooks", "ck_playbooks_version"),
        ("playbook_versions", "ck_playbook_versions_author"),
        ("playbook_versions", "ck_playbook_versions_version"),
    ):
        assert f"CONSTRAINT {name} CHECK ({modelled[(table, name)]})" in sql, name
    action = "ck_chat_approvals_action"
    assert f"ADD CONSTRAINT {action} CHECK ({modelled[('chat_approvals', action)]})" in sql
    assert modelled[("chat_approvals", action)] == f"action IN ({ACTIONS_0046})"
    for table in (Playbook.__table__, PlaybookVersion.__table__):
        assert all(f"{c.name} " in sql for c in table.columns), table.name
    uniques = {
        table.name: {c.name for c in table.constraints if isinstance(c, UniqueConstraint)}
        for table in (Playbook.__table__, PlaybookVersion.__table__)
    }
    assert uniques == {
        "playbooks": {"uq_playbooks_user_slug"},
        "playbook_versions": {"uq_playbook_versions_playbook_version"},
    }


def test_the_schema_literal_matches_the_check():
    assert ", ".join(f"'{a}'" for a in get_args(ChatAction)) == ACTIONS_0046


# ── a seed: the text a first save replaces ───────────────────────────────────

SEED = {"seed_body": "# The shipped text\n", "seed_note": "the shipped text, version 3", "based_on_shipped_version": 3}


def test_a_seeded_first_save_is_version_2_over_the_seed(env):
    out = _saved(env, **SEED, body="# Mine\n", note="tighter", author="agent")
    assert (out["version"], out["body"], out["based_on_shipped_version"]) == (2, "# Mine\n", 3)
    assert env.client.get(_url(), headers=_h()).json() == out
    listed = _versions(env)
    assert [(v["version"], v["note"], v["author"]) for v in listed] == [
        (2, "tighter", "agent"), (1, "the shipped text, version 3", "user"),
    ]
    one = env.client.get(_url(tail="/versions/1"), headers=_h()).json()
    assert one["body"] == "# The shipped text\n"
    assert env.client.get(_url(tail="/versions/2"), headers=_h()).json()["body"] == "# Mine\n"
    assert env.count(Playbook) == 1 and env.count(PlaybookVersion) == 2
    # The playbook goes on from version 2.
    assert _saved(env, expect_version=2, body="# Mine again\n")["version"] == 3


def test_a_seed_needs_no_note_and_may_be_empty(env):
    _saved(env, seed_body="")
    first = env.client.get(_url(tail="/versions/1"), headers=_h()).json()
    assert (first["body"], first["note"], first["author"]) == ("", "", "user")
    assert env.client.get(_url(), headers=_h()).json()["version"] == 2


def test_without_a_seed_a_first_save_is_still_version_1(env):
    assert _saved(env, seed_body=None, seed_note=None)["version"] == 1
    assert [v["version"] for v in _versions(env)] == [1]


def test_a_seed_is_only_for_a_create(env):
    _saved(env)
    for over in ({"seed_body": "old"}, {"seed_note": "why"}, {"seed_body": "old", "seed_note": "why"}):
        r = _save(env, expect_version=1, body="next", **over)
        assert r.status_code == 422, over
    assert env.client.get(_url(), headers=_h()).json()["version"] == 1
    assert env.count(PlaybookVersion) == 1


def test_a_seed_note_needs_a_seed_body(env):
    assert _save(env, seed_note="why").status_code == 422
    assert env.count(Playbook) == 0 and env.count(PlaybookVersion) == 0


def test_a_seed_body_has_the_bodys_limit_and_type(env):
    assert _save(env, seed_body="x" * 32001).status_code == 422
    assert _save(env, seed_body=7).status_code == 422
    assert env.count(Playbook) == 0 and env.count(PlaybookVersion) == 0
    assert _save(env, seed_body="x" * 32000).status_code == 200
    assert len(env.client.get(_url(tail="/versions/1"), headers=_h()).json()["body"]) == 32000


def test_a_seeded_first_save_over_an_existing_playbook_is_stale_and_changes_nothing(env):
    _saved(env, **SEED, body="tab B")
    again = _save(env, **SEED, body="tab A")
    assert (again.status_code, again.json()["detail"]) == (409, "stale_playbook_version")
    assert env.client.get(_url(), headers=_h()).json()["body"] == "tab B"
    assert env.count(Playbook) == 1 and env.count(PlaybookVersion) == 2


def test_a_seeded_first_save_is_one_transaction(env):
    """The second version row fails after the playbook and the seed's row were written: nothing stays."""

    def fail_on_version_2(_mapper, _connection, target):
        if target.version == 2:
            raise RuntimeError("the disk is full")

    event.listen(PlaybookVersion, "before_insert", fail_on_version_2)
    try:
        with pytest.raises(RuntimeError):
            _save(env, **SEED)
    finally:
        event.remove(PlaybookVersion, "before_insert", fail_on_version_2)
    assert env.count(Playbook) == 0 and env.count(PlaybookVersion) == 0
    assert env.client.get(_url(), headers=_h()).status_code == 404
    # Nothing is left in the way of the next try.
    assert _saved(env, **SEED)["version"] == 2
