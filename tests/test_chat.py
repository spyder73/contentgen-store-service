"""Operator chat: threads, messages and one-time spend approvals (migration 0042).

Three tables back the operator chat: ``chat_threads`` (one conversation, with the
transcript the operator resumes from), ``chat_messages`` (the visible log, ``seq``
assigned by the store) and ``chat_approvals`` (a spend the user must approve with a
button; the button mints a token, only its sha256 is stored, and ``consume`` turns it
into ``used`` exactly once).

The behaviour tests drive the real FastAPI app over a real sqlite database (a file,
because the TestClient and the test setup run on different event loops), so the
headers, response models, ownership scoping and error translation are all exercised.
The migration's own DDL is asserted by rendering it offline in the postgres dialect.
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import NullPool


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(element, compiler, **kw):  # pragma: no cover - trivial
    return "JSON"


@compiles(UUID, "sqlite")
def _uuid_sqlite(element, compiler, **kw):  # pragma: no cover - trivial
    return "VARCHAR(36)"


from app.db import get_session  # noqa: E402
from app.fastapi_app import create_fastapi_app  # noqa: E402
from app.models import ChatApproval, ChatMessage, ChatThread, User  # noqa: E402
from app.stores import chat as chat_store  # noqa: E402
from app.stores.access import DEFAULT_FEATURES, TAB_FEATURES  # noqa: E402

SECRET = "chat-test-secret"
USER_A = str(uuid.uuid4())
USER_B = str(uuid.uuid4())

MIGRATION_PATH = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0042_chat.py"


def _h(user: str | None = USER_A) -> dict[str, str]:
    headers = {"X-Internal-Secret": SECRET}
    if user:
        headers["X-User-ID"] = user
    return headers


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _in(minutes: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()


class Env:
    """A TestClient over the real app plus direct database access for assertions."""

    def __init__(self, client: TestClient, factory) -> None:
        self.client = client
        self.factory = factory

    def db(self, fn):
        """Run ``async fn(session)`` against the same database the app uses."""

        async def run():
            async with self.factory() as session:
                return await fn(session)

        return asyncio.run(run())

    def count(self, model, *where) -> int:
        async def q(s):
            return (await s.execute(select(func.count()).select_from(model).where(*where))).scalar_one()

        return self.db(q)

    # thread / message / approval helpers ---------------------------------------

    def thread(self, user: str = USER_A, **body) -> dict:
        r = self.client.post("/v1/chat/threads", json=body, headers=_h(user))
        assert r.status_code == 200, r.text
        return r.json()

    def message(self, thread_id: str, user: str = USER_A, **body) -> dict:
        body = {"role": "user", "text": "hi", **body}
        r = self.client.post(f"/v1/chat/threads/{thread_id}/messages", json=body, headers=_h(user))
        assert r.status_code == 200, r.text
        return r.json()

    def approval(self, thread_id: str, user: str = USER_A, **body) -> dict:
        body = {"action": "run.start", "subject": "series-1", "amount_usd": 1.0, "summary": "Run it", **body}
        r = self.client.post(f"/v1/chat/threads/{thread_id}/approvals", json=body, headers=_h(user))
        assert r.status_code == 200, r.text
        return r.json()

    def approve(self, approval_id: str, token: str, user: str = USER_A, expires_in: float = 10):
        return self.client.post(
            f"/v1/chat/approvals/{approval_id}/approve",
            json={"token_sha256": _sha(token), "expires_at": _in(expires_in)},
            headers=_h(user),
        )

    def approved(self, user: str = USER_A, *, token: str | None = None, **body):
        """A thread with one approved approval; returns (thread_id, approval_id, token)."""
        token = token or uuid.uuid4().hex
        thread = self.thread(user)
        approval = self.approval(thread["id"], user, **body)
        assert self.approve(approval["id"], token, user).status_code == 200
        return thread["id"], approval["id"], token

    def consume(self, body: dict, user: str | None = USER_A):
        return self.client.post("/v1/chat/approvals/consume", json=body, headers=_h(user))

    def set_row(self, model, row_id: str, **values) -> None:
        async def q(s):
            await s.execute(update(model).where(model.id == row_id).values(**values))
            await s.commit()

        self.db(q)


async def _create_tables(engine) -> None:
    async with engine.begin() as conn:
        for table in (User.__table__, ChatThread.__table__, ChatMessage.__table__, ChatApproval.__table__):
            await conn.run_sync(lambda c, t=table: t.create(c))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        s.add(User(id=USER_A, username="a", password_hash="x"))
        s.add(User(id=USER_B, username="b", password_hash="x"))
        await s.commit()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("INTERNAL_API_SECRET", SECRET)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'chat.db'}", poolclass=NullPool)
    asyncio.run(_create_tables(engine))
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _session():
        async with factory() as session:
            yield session

    app = create_fastapi_app()
    app.dependency_overrides[get_session] = _session
    with TestClient(app) as client:
        yield Env(client, factory)
    asyncio.run(engine.dispose())


# ── threads ──────────────────────────────────────────────────────────────────


def test_a_new_thread_starts_idle_and_empty(env):
    t = env.thread(title="Ideas")
    assert t["title"] == "Ideas"
    assert (t["provider"], t["model"], t["series_id"]) == ("", "", None)
    assert (t["transcript"], t["known_media"], t["status"], t["cost_usd"]) == ([], [], "idle", 0)
    assert env.client.get(f"/v1/chat/threads/{t['id']}", headers=_h()).json()["id"] == t["id"]


def test_thread_crud_is_scoped_to_its_user(env):
    t = env.thread(title="mine")
    url = f"/v1/chat/threads/{t['id']}"
    assert env.client.get(url, headers=_h(USER_B)).status_code == 404
    assert env.client.patch(url, json={"title": "stolen"}, headers=_h(USER_B)).status_code == 404
    assert env.client.delete(url, headers=_h(USER_B)).status_code == 404
    assert env.client.get("/v1/chat/threads", headers=_h(USER_B)).json() == []
    # A's thread is untouched by B's attempts.
    assert env.client.get(url, headers=_h()).json()["title"] == "mine"
    assert env.client.delete(url, headers=_h()).status_code == 204
    assert env.client.get(url, headers=_h()).status_code == 404


def test_an_id_that_is_not_a_uuid_is_a_404_not_a_server_error(env):
    assert env.client.get("/v1/chat/threads/not-a-uuid", headers=_h()).status_code == 404
    assert env.client.get("/v1/chat/approvals/not-a-uuid", headers=_h()).status_code == 404
    assert env.client.patch("/v1/chat/messages/not-a-uuid", json={"text": "x"}, headers=_h()).status_code == 404
    body = {"thread_id": "nope", "token_sha256": _sha("t"), "action": "run.start", "subject": "s", "needed_usd": 1}
    r = env.consume(body)
    assert (r.status_code, r.json()["detail"]) == (409, "unknown_token")


def test_list_threads_newest_first_without_transcript(env):
    old = env.thread(title="old", transcript=[{"user": "hello", "operator": "hi"}])
    new = env.thread(title="new", transcript=[{"user": "x"}])
    listed = env.client.get("/v1/chat/threads", headers=_h()).json()
    assert [t["id"] for t in listed] == [new["id"], old["id"]]
    assert all("transcript" not in t and "known_media" not in t for t in listed)
    assert listed[0]["title"] == "new" and listed[0]["status"] == "idle"
    # Activity moves a thread to the top: a message bumps updated_at.
    env.message(old["id"])
    assert [t["id"] for t in env.client.get("/v1/chat/threads", headers=_h()).json()] == [old["id"], new["id"]]
    # Paging.
    page = env.client.get("/v1/chat/threads?limit=1&offset=1", headers=_h()).json()
    assert [t["id"] for t in page] == [new["id"]]


def test_patch_thread_sets_fields_and_leaves_the_rest(env):
    t = env.thread(title="a", provider="p", model="m", series_id=str(uuid.uuid4()))
    sid = str(uuid.uuid4())
    body = {
        "title": "b", "provider": "p2", "model": "m2", "series_id": sid, "status": "working",
        "transcript": [{"user": "q", "operator": "a"}], "known_media": [{"id": "m1"}],
    }
    r = env.client.patch(f"/v1/chat/threads/{t['id']}", json=body, headers=_h())
    assert r.status_code == 200
    out = r.json()
    assert {k: out[k] for k in body} == body
    # An empty patch changes nothing; an explicit null series_id unbinds the thread.
    assert env.client.patch(f"/v1/chat/threads/{t['id']}", json={}, headers=_h()).json()["series_id"] == sid
    cleared = env.client.patch(f"/v1/chat/threads/{t['id']}", json={"series_id": None}, headers=_h()).json()
    assert cleared["series_id"] is None and cleared["title"] == "b" and cleared["transcript"] == body["transcript"]


def test_patch_thread_rejects_a_bad_status_and_a_negative_cost(env):
    t = env.thread()
    url = f"/v1/chat/threads/{t['id']}"
    assert env.client.patch(url, json={"status": "busy"}, headers=_h()).status_code == 422
    assert env.client.patch(url, json={"add_cost_usd": -0.1}, headers=_h()).status_code == 422


def test_patch_thread_adds_cost_atomically(env):
    t = env.thread()
    url = f"/v1/chat/threads/{t['id']}"
    env.client.patch(url, json={"add_cost_usd": 0.1}, headers=_h())
    out = env.client.patch(url, json={"add_cost_usd": 0.1, "title": "x"}, headers=_h()).json()
    assert out["cost_usd"] == pytest.approx(0.2)


def test_two_stale_cost_increments_both_land(env):
    """Two writers that each loaded cost 0 must end at 0.2, not 0.1."""
    t = env.thread()

    async def run():
        async with env.factory() as s1, env.factory() as s2:
            await chat_store.get_thread(s1, USER_A, t["id"])
            await chat_store.get_thread(s2, USER_A, t["id"])
            from app.schemas import ChatThreadPatch

            await chat_store.patch_thread(s1, USER_A, t["id"], ChatThreadPatch(add_cost_usd=0.1))
            await chat_store.patch_thread(s2, USER_A, t["id"], ChatThreadPatch(add_cost_usd=0.1))

    asyncio.run(run())
    assert env.client.get(f"/v1/chat/threads/{t['id']}", headers=_h()).json()["cost_usd"] == pytest.approx(0.2)


def test_caller_minted_thread_id_is_idempotent_and_not_stealable(env):
    tid = str(uuid.uuid4())
    a = env.thread(id=tid, title="first")
    again = env.thread(id=tid, title="second")
    assert again["id"] == a["id"] == tid and again["title"] == "first"
    r = env.client.post("/v1/chat/threads", json={"id": tid}, headers=_h(USER_B))
    assert r.status_code == 409 and r.json()["detail"] == "thread_id_taken"
    assert env.client.post("/v1/chat/threads", json={"id": "not-a-uuid"}, headers=_h()).status_code == 422


# ── messages ─────────────────────────────────────────────────────────────────


def test_messages_get_increasing_seq_and_list_after_seq(env):
    t = env.thread()
    for text in ("one", "two", "three"):
        env.message(t["id"], text=text)
    url = f"/v1/chat/threads/{t['id']}/messages"
    listed = env.client.get(url, headers=_h()).json()
    assert [(m["seq"], m["text"]) for m in listed] == [(1, "one"), (2, "two"), (3, "three")]
    assert [m["seq"] for m in env.client.get(url + "?after_seq=1", headers=_h()).json()] == [2, 3]
    assert [m["seq"] for m in env.client.get(url + "?after_seq=1&limit=1", headers=_h()).json()] == [2]
    assert env.client.get(url + "?after_seq=3", headers=_h()).json() == []
    # seq is per thread.
    other = env.thread()
    assert env.message(other["id"])["seq"] == 1


def test_a_message_has_a_role_kind_text_and_data(env):
    t = env.thread()
    m = env.message(t["id"], role="operator", kind="tool", text="", data={"name": "x", "args": {"n": 1}})
    assert (m["role"], m["kind"], m["data"]) == ("operator", "tool", {"name": "x", "args": {"n": 1}})
    assert m["thread_id"] == t["id"] and m["created_at"] and m["updated_at"]
    plain = env.message(t["id"])
    assert (plain["role"], plain["kind"], plain["data"]) == ("user", "text", {})
    url = f"/v1/chat/threads/{t['id']}/messages"
    for bad in ({"role": "system"}, {"kind": "poem"}):
        assert env.client.post(url, json={"role": "user", **bad}, headers=_h()).status_code == 422


def test_messages_are_scoped_to_the_threads_owner(env):
    t = env.thread()
    m = env.message(t["id"])
    url = f"/v1/chat/threads/{t['id']}/messages"
    assert env.client.get(url, headers=_h(USER_B)).status_code == 404
    assert env.client.post(url, json={"role": "user", "text": "x"}, headers=_h(USER_B)).status_code == 404
    assert env.client.patch(f"/v1/chat/messages/{m['id']}", json={"text": "x"}, headers=_h(USER_B)).status_code == 404
    assert env.client.get(url, headers=_h()).json()[0]["text"] == "hi"


def test_a_caller_minted_message_id_is_idempotent_and_belongs_to_one_thread(env):
    t, other = env.thread(), env.thread()
    mid = str(uuid.uuid4())
    first = env.message(t["id"], id=mid, text="once")
    again = env.message(t["id"], id=mid, text="retry")
    assert again["id"] == first["id"] == mid and again["seq"] == 1 and again["text"] == "once"
    assert len(env.client.get(f"/v1/chat/threads/{t['id']}/messages", headers=_h()).json()) == 1
    r = env.client.post(
        f"/v1/chat/threads/{other['id']}/messages", json={"id": mid, "role": "user"}, headers=_h()
    )
    assert r.status_code == 409 and r.json()["detail"] == "message_id_taken"


def test_a_lost_seq_race_retries_with_the_next_seq(env, monkeypatch):
    """Two writers that both computed max+1: the unique (thread, seq) refuses the loser, which retries."""
    t = env.thread()
    env.message(t["id"], text="first")
    real = chat_store._next_seq
    calls = {"n": 0}

    async def stale_once(session, thread_id):
        calls["n"] += 1
        return 1 if calls["n"] == 1 else await real(session, thread_id)

    monkeypatch.setattr(chat_store, "_next_seq", stale_once)
    m = env.message(t["id"], text="second")
    assert m["seq"] == 2 and calls["n"] == 2


def test_patch_message_replaces_data_and_keeps_what_it_omits(env):
    t = env.thread()
    m = env.message(t["id"], kind="approval", text="Spend $1?", data={"a": 1, "b": 2})
    url = f"/v1/chat/messages/{m['id']}"
    out = env.client.patch(url, json={"data": {"c": 3}}, headers=_h()).json()
    assert out["data"] == {"c": 3} and out["text"] == "Spend $1?" and out["seq"] == m["seq"]
    out = env.client.patch(url, json={"text": "Done"}, headers=_h()).json()
    assert out["text"] == "Done" and out["data"] == {"c": 3}
    assert env.client.patch(url, json={"text": "x"}, headers=_h(USER_B)).status_code == 404
    assert env.client.patch(f"/v1/chat/messages/{uuid.uuid4()}", json={"text": "x"}, headers=_h()).status_code == 404


def test_deleting_a_thread_deletes_its_messages_and_approvals(env):
    doomed, kept = env.thread(), env.thread()
    for t in (doomed, kept):
        env.message(t["id"])
        env.message(t["id"])
        env.approval(t["id"])
    assert env.count(ChatMessage) == 4 and env.count(ChatApproval) == 2
    assert env.client.delete(f"/v1/chat/threads/{doomed['id']}", headers=_h()).status_code == 204
    assert env.count(ChatMessage, ChatMessage.thread_id == doomed["id"]) == 0
    assert env.count(ChatApproval, ChatApproval.thread_id == doomed["id"]) == 0
    assert env.count(ChatMessage) == 2 and env.count(ChatApproval) == 1


# ── approvals: create, approve, decline ──────────────────────────────────────


def test_an_approval_starts_pending_and_never_shows_a_token_hash(env):
    t = env.thread()
    msg = env.message(t["id"], kind="approval")
    a = env.approval(t["id"], message_id=msg["id"], breakdown={"lines": [{"label": "video", "usd": 1.0}]})
    assert (a["status"], a["thread_id"], a["message_id"]) == ("pending", t["id"], msg["id"])
    assert (a["action"], a["subject"], a["amount_usd"], a["summary"]) == ("run.start", "series-1", 1.0, "Run it")
    assert a["breakdown"] == {"lines": [{"label": "video", "usd": 1.0}]}
    assert a["expires_at"] is None and a["decided_at"] is None and a["used_at"] is None
    assert "token_sha256" not in a
    got = env.client.get(f"/v1/chat/approvals/{a['id']}", headers=_h())
    assert got.status_code == 200 and got.json() == a
    assert env.client.get(f"/v1/chat/approvals/{a['id']}", headers=_h(USER_B)).status_code == 404
    url = f"/v1/chat/threads/{t['id']}/approvals"
    assert env.client.post(url, json={"action": "run.start", "amount_usd": 1}, headers=_h(USER_B)).status_code == 404


def test_approval_input_is_validated(env):
    t = env.thread()
    url = f"/v1/chat/threads/{t['id']}/approvals"
    ok = {"action": "image.generate", "subject": "s", "amount_usd": 0}
    assert env.client.post(url, json=ok, headers=_h()).status_code == 200
    for bad in ({"action": "run.delete"}, {"amount_usd": -0.01}):
        assert env.client.post(url, json={**ok, **bad}, headers=_h()).status_code == 422


def test_approve_only_from_pending(env):
    t = env.thread()
    a = env.approval(t["id"])
    first = env.approve(a["id"], "tok")
    assert first.status_code == 200
    body = first.json()
    assert body["status"] == "approved" and body["decided_at"] and body["expires_at"]
    assert "token_sha256" not in body
    second = env.approve(a["id"], "tok2")
    assert (second.status_code, second.json()["detail"]) == (409, "approval_not_pending")
    declined = env.client.post(f"/v1/chat/approvals/{a['id']}/decline", headers=_h())
    assert (declined.status_code, declined.json()["detail"]) == (409, "approval_not_pending")
    # The first approval's token is the one that counts.
    assert env.client.get(f"/v1/chat/approvals/{a['id']}", headers=_h()).json()["status"] == "approved"


def test_decline_only_from_pending(env):
    t = env.thread()
    a = env.approval(t["id"])
    r = env.client.post(f"/v1/chat/approvals/{a['id']}/decline", headers=_h())
    assert r.status_code == 200 and r.json()["status"] == "declined" and r.json()["decided_at"]
    again = env.client.post(f"/v1/chat/approvals/{a['id']}/decline", headers=_h())
    assert (again.status_code, again.json()["detail"]) == (409, "approval_not_pending")
    late = env.approve(a["id"], "tok")
    assert (late.status_code, late.json()["detail"]) == (409, "approval_not_pending")


def test_approve_and_decline_are_scoped_to_the_threads_owner(env):
    t = env.thread()
    a = env.approval(t["id"])
    assert env.approve(a["id"], "tok", user=USER_B).status_code == 404
    assert env.client.post(f"/v1/chat/approvals/{a['id']}/decline", headers=_h(USER_B)).status_code == 404
    assert env.approve(str(uuid.uuid4()), "tok").status_code == 404
    assert env.client.get(f"/v1/chat/approvals/{a['id']}", headers=_h()).json()["status"] == "pending"


def test_approve_needs_a_token_hash_and_an_expiry(env):
    a = env.approval(env.thread()["id"])
    url = f"/v1/chat/approvals/{a['id']}/approve"
    assert env.client.post(url, json={"expires_at": _in(5)}, headers=_h()).status_code == 422
    assert env.client.post(url, json={"token_sha256": _sha("t")}, headers=_h()).status_code == 422
    assert env.client.post(url, json={"token_sha256": "", "expires_at": _in(5)}, headers=_h()).status_code == 422


def test_a_token_hash_cannot_back_two_approvals_in_one_thread(env):
    t = env.thread()
    first, second = env.approval(t["id"]), env.approval(t["id"])
    assert env.approve(first["id"], "same").status_code == 200
    r = env.approve(second["id"], "same")
    assert (r.status_code, r.json()["detail"]) == (409, "token_taken")
    assert env.client.get(f"/v1/chat/approvals/{second['id']}", headers=_h()).json()["status"] == "pending"
    assert env.approve(second["id"], "different").status_code == 200


# ── approvals: consume ───────────────────────────────────────────────────────


def _consume_body(thread_id: str, token: str, **over) -> dict:
    return {
        "thread_id": thread_id, "token_sha256": _sha(token), "action": "run.start",
        "subject": "series-1", "needed_usd": 1.0, **over,
    }


def test_consume_is_one_time(env):
    thread_id, approval_id, token = env.approved()
    body = _consume_body(thread_id, token)
    r = env.consume(body)
    assert r.status_code == 200
    assert r.json()["status"] == "used" and r.json()["used_at"] and r.json()["id"] == approval_id
    again = env.consume(body)
    assert (again.status_code, again.json()["detail"]) == (409, "used")
    assert env.client.get(f"/v1/chat/approvals/{approval_id}", headers=_h()).json()["status"] == "used"


def test_consume_accepts_what_the_approval_covers_within_half_a_cent(env):
    thread_id, _, token = env.approved(amount_usd=1.0)
    assert env.consume(_consume_body(thread_id, token, needed_usd=1.005)).status_code == 200
    thread_id, _, token = env.approved(amount_usd=1.0)
    assert env.consume(_consume_body(thread_id, token, needed_usd=0)).status_code == 200
    thread_id, _, token = env.approved(amount_usd=1.0)
    r = env.consume(_consume_body(thread_id, token, needed_usd=1.006))
    assert (r.status_code, r.json()["detail"]) == (409, "amount_exceeded")


class _Case:
    def __init__(self, env: Env, user: str) -> None:
        self.env = env
        self.user = user
        self.thread_id, self.approval_id, self.token = env.approved(user)
        self.body = _consume_body(self.thread_id, self.token)
        self.stays = "approved"

    def row(self, **values) -> None:
        self.env.set_row(ChatApproval, self.approval_id, **values)


def _other_thread(c: _Case) -> None:
    c.body["thread_id"] = c.env.thread(c.user)["id"]


def _other_users_thread(c: _Case) -> None:
    c.body["thread_id"] = c.env.thread(USER_B)["id"]


@pytest.mark.parametrize(
    "change,reason",
    [
        pytest.param(lambda c: c.body.update(token_sha256=_sha("invented")), "unknown_token", id="invented-token"),
        pytest.param(lambda c: c.body.update(token_sha256=""), "unknown_token", id="empty-token"),
        pytest.param(_other_thread, "unknown_token", id="token-from-another-thread"),
        pytest.param(_other_users_thread, "unknown_token", id="another-users-thread"),
        pytest.param(lambda c: c.row(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)), "expired", id="expired"),
        pytest.param(lambda c: c.body.update(action="image.generate"), "wrong_action", id="wrong-action"),
        pytest.param(lambda c: c.body.update(subject="series-2"), "wrong_subject", id="wrong-subject"),
        pytest.param(lambda c: c.body.update(needed_usd=1.01), "amount_exceeded", id="amount-exceeded"),
        # A row whose status moved off "approved" after its hash was stored.
        pytest.param(lambda c: c.row(status="pending"), "pending", id="pending"),
        pytest.param(lambda c: c.row(status="declined"), "declined", id="declined"),
        pytest.param(lambda c: c.row(status="used"), "used", id="used"),
        pytest.param(lambda c: c.row(status="expired"), "expired", id="status-expired"),
    ],
)
def test_consume_refuses_and_names_why(env, change, reason):
    case = _Case(env, USER_A)
    change(case)
    before = env.client.get(f"/v1/chat/approvals/{case.approval_id}", headers=_h()).json()
    r = env.consume(case.body)
    assert r.status_code == 409
    assert r.json()["detail"] == reason
    after = env.client.get(f"/v1/chat/approvals/{case.approval_id}", headers=_h()).json()
    # A refusal changes nothing: an amount_exceeded row stays approved, not spent.
    assert after == before


def test_an_amount_exceeded_refusal_leaves_the_approval_usable(env):
    thread_id, approval_id, token = env.approved(amount_usd=2.0)
    r = env.consume(_consume_body(thread_id, token, needed_usd=2.5))
    assert (r.status_code, r.json()["detail"]) == (409, "amount_exceeded")
    row = env.client.get(f"/v1/chat/approvals/{approval_id}", headers=_h()).json()
    assert row["status"] == "approved" and row["used_at"] is None
    assert env.consume(_consume_body(thread_id, token, needed_usd=2.0)).status_code == 200


def test_a_token_works_for_its_own_user_only(env):
    thread_id, approval_id, token = env.approved(USER_A)
    r = env.consume(_consume_body(thread_id, token), user=USER_B)
    assert (r.status_code, r.json()["detail"]) == (409, "unknown_token")
    assert env.client.get(f"/v1/chat/approvals/{approval_id}", headers=_h()).json()["status"] == "approved"


def test_an_approval_granted_with_a_past_expiry_is_expired_at_consume(env):
    thread = env.thread()
    a = env.approval(thread["id"])
    assert env.approve(a["id"], "tok", expires_in=-5).status_code == 200
    r = env.consume(_consume_body(thread["id"], "tok"))
    assert (r.status_code, r.json()["detail"]) == (409, "expired")


def test_consume_needs_a_user_and_a_complete_body(env):
    thread_id, _, token = env.approved()
    assert env.consume(_consume_body(thread_id, token), user=None).status_code == 401
    for key in ("thread_id", "token_sha256", "action", "subject", "needed_usd"):
        body = _consume_body(thread_id, token)
        del body[key]
        assert env.consume(body).status_code == 422, key
    assert env.consume(_consume_body(thread_id, token, needed_usd=-1)).status_code == 422
    assert env.consume(_consume_body(thread_id, token)).status_code == 200


def test_two_consumes_that_both_saw_approved_spend_it_once(env, monkeypatch):
    """The row is selected, checked, then updated; the update itself must refuse a lost race."""
    thread_id, approval_id, token = env.approved()
    real = chat_store._approval_by_token
    outcome: dict = {}

    async def racing(session, user_id, thread_id_, token_sha256):
        row = await real(session, user_id, thread_id_, token_sha256)
        if "rival" not in outcome:
            outcome["rival"] = "ran"
            async with env.factory() as other:
                from app.schemas import ChatApprovalConsume

                outcome["winner"] = await chat_store.consume_approval(
                    other, user_id, ChatApprovalConsume(**_consume_body(thread_id, token))
                )
        return row

    async def run():
        monkeypatch.setattr(chat_store, "_approval_by_token", racing)
        from app.schemas import ChatApprovalConsume

        async with env.factory() as loser:
            with pytest.raises(chat_store.ChatError) as caught:
                await chat_store.consume_approval(loser, USER_A, ChatApprovalConsume(**_consume_body(thread_id, token)))
        return caught.value

    err = asyncio.run(run())
    assert outcome["winner"].status == "used"
    assert (err.status_code, err.message) == (409, "used")
    assert env.count(ChatApproval, ChatApproval.status == "used") == 1


def test_chat_routes_need_a_user_and_the_internal_secret(env):
    t = env.thread()
    for method, path in (
        ("get", "/v1/chat/threads"), ("get", f"/v1/chat/threads/{t['id']}"),
        ("get", f"/v1/chat/threads/{t['id']}/messages"), ("get", f"/v1/chat/approvals/{uuid.uuid4()}"),
        ("post", f"/v1/chat/approvals/{uuid.uuid4()}/decline"),
    ):
        assert getattr(env.client, method)(path, headers=_h(None)).status_code == 401, path
        no_secret = {"X-User-ID": USER_A}
        assert getattr(env.client, method)(path, headers=no_secret).status_code == 401, path


# ── access feature ───────────────────────────────────────────────────────────


def test_chat_is_a_default_whitelisted_feature():
    assert ("chat", True) in DEFAULT_FEATURES and TAB_FEATURES["chat"] == "chat"


# ── migration ────────────────────────────────────────────────────────────────


class _Buffer:
    """Collects alembic's offline DDL instead of printing it to stdout."""

    def __init__(self, sink: list[str]) -> None:
        self._sink = sink

    def write(self, value: str) -> None:
        self._sink.append(value)

    def flush(self) -> None:  # pragma: no cover - trivial
        return None


def _load_migration_module():
    spec = importlib.util.spec_from_file_location("migration_0042_chat", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _render(fn) -> str:
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy.dialects import postgresql

    statements: list[str] = []
    ctx = MigrationContext.configure(
        dialect=postgresql.dialect(),
        opts={"as_sql": True, "output_buffer": _Buffer(statements)},
    )
    with Operations.context(ctx):
        fn()
    return " ".join(" ".join(statements).split())


def test_migration_0042_chains_onto_0041():
    module = _load_migration_module()
    assert module.revision == "0042" and module.down_revision == "0041"


def test_migration_0042_renders_the_postgres_ddl():
    sql = _render(_load_migration_module().upgrade)
    assert all(f"CREATE TABLE {t}" in sql for t in ("chat_threads", "chat_messages", "chat_approvals"))
    # Owner and thread links cascade; the JSONB columns default to an empty list / object.
    assert sql.count("REFERENCES users (id) ON DELETE CASCADE") == 1
    assert sql.count("REFERENCES chat_threads (id) ON DELETE CASCADE") == 2
    assert "transcript JSONB DEFAULT '[]'::jsonb NOT NULL" in sql
    assert "known_media JSONB DEFAULT '[]'::jsonb NOT NULL" in sql
    assert "data JSONB DEFAULT '{}'::jsonb NOT NULL" in sql
    assert "CREATE INDEX ix_chat_threads_user_updated ON chat_threads (user_id, updated_at)" in sql
    assert "UNIQUE (thread_id, seq)" in sql
    assert "CHECK (amount_usd >= 0)" in sql
    for values in ("'idle', 'working'", "'user', 'operator'", "'text', 'tool', 'approval', 'run', 'media', 'error'",
                   "'run.start', 'run.continue', 'image.generate', 'clip.render'",
                   "'pending', 'approved', 'used', 'declined', 'expired'"):
        assert f"IN ({values})" in sql, values


def test_migration_0042_downgrade_drops_the_three_tables():
    sql = _render(_load_migration_module().downgrade)
    for table in ("chat_approvals", "chat_messages", "chat_threads"):
        assert f"DROP TABLE {table}" in sql
    assert sql.index("DROP TABLE chat_approvals") < sql.index("DROP TABLE chat_threads")
    assert sql.index("DROP TABLE chat_messages") < sql.index("DROP TABLE chat_threads")


def test_the_models_declare_the_indexes_and_constraints_of_the_migration():
    """Model/migration drift means a future autogenerate "fixes" production."""
    import re

    source = MIGRATION_PATH.read_text()
    declared = set(re.findall(r'op\.create_index\(\s*"([^"]+)"', source))
    modelled = {ix.name for t in (ChatThread, ChatMessage, ChatApproval) for ix in t.__table__.indexes}
    assert modelled == declared
    constraints = {c.name for t in (ChatThread, ChatMessage, ChatApproval) for c in t.__table__.constraints}
    for name in re.findall(r'name="((?:ck|ux)_chat_[a-z_]+)"', source):
        assert name in constraints, name
