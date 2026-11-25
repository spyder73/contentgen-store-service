"""Telegram recipients per user, and the recipients a chat sends to (migration 0057)."""
from __future__ import annotations

import pytest

from tests.test_chat import USER_A, USER_B, _h, env  # noqa: F401  (env is the fixture)


def _url(tid: str = "") -> str:
    return f"/v1/users/{USER_A}/telegram-recipients" + (f"/{tid}" if tid else "")


def _put(env, tid: str, user: str = USER_A, **body):
    return env.client.put(f"/v1/users/{user}/telegram-recipients/{tid}", json={"username": "", **body}, headers=_h(user))


def _targets(env, thread_id: str, targets: list[str], user: str = USER_A):
    return env.client.patch(f"/v1/chat/threads/{thread_id}", json={"telegram_targets": targets}, headers=_h(user))


def test_put_then_list(env):
    r = _put(env, "111", username="ann")
    assert r.status_code == 200 and r.json()["telegram_id"] == "111" and r.json()["username"] == "ann"
    _put(env, "222")
    got = env.client.get(_url(), headers=_h()).json()
    assert [(g["telegram_id"], g["username"]) for g in got] == [("111", "ann"), ("222", "")]
    assert set(got[0]) == {"telegram_id", "username", "created_at"}


def test_put_again_renames_without_a_duplicate(env):
    first = _put(env, "111", username="ann").json()
    again = _put(env, "111", username="anna").json()
    assert again["username"] == "anna" and again["created_at"] == first["created_at"]
    assert len(env.client.get(_url(), headers=_h()).json()) == 1


def test_twenty_recipients_at_most(env):
    for n in range(20):
        assert _put(env, str(n)).status_code == 200
    r = _put(env, "99")
    assert r.status_code == 409 and r.json()["detail"] == "recipient_limit"
    assert _put(env, "5", username="still ok").status_code == 200  # a rename is not a 21st


def test_another_users_recipients_are_never_listed(env):
    _put(env, "111", user=USER_B)
    assert env.client.get(_url(), headers=_h()).json() == []
    assert env.client.delete(_url("111"), headers=_h()).status_code == 404


@pytest.mark.parametrize("bad", ["abc", "12a", "1" * 21, "-5"])
def test_an_id_that_is_not_digits_is_refused(env, bad):
    assert _put(env, bad).status_code == 422
    assert env.client.delete(_url(bad), headers=_h()).status_code == 422


def test_a_username_over_64_characters_is_refused(env):
    assert _put(env, "1", username="x" * 65).status_code == 422


def test_delete_strips_the_id_from_the_users_threads_only(env):
    _put(env, "111")
    _put(env, "222")
    _put(env, "111", user=USER_B)
    mine, theirs = env.thread(), env.thread(USER_B)
    assert _targets(env, mine["id"], ["111", "222"]).status_code == 200
    assert _targets(env, theirs["id"], ["111"], USER_B).status_code == 200
    assert env.client.delete(_url("111"), headers=_h()).status_code == 204
    get = lambda t, u: env.client.get(f"/v1/chat/threads/{t['id']}", headers=_h(u)).json()["telegram_targets"]  # noqa: E731
    assert get(mine, USER_A) == ["222"] and get(theirs, USER_B) == ["111"]
    assert [r["telegram_id"] for r in env.client.get(_url(), headers=_h()).json()] == ["222"]
    assert env.client.delete(_url("111"), headers=_h()).status_code == 404


def test_targets_must_be_the_users_recipients(env):
    _put(env, "111")
    _put(env, "222")
    thread = env.thread()
    bad = _targets(env, thread["id"], ["111", "999"])
    assert bad.status_code == 422 and bad.json()["detail"] == "unknown_recipient"
    assert env.client.get(f"/v1/chat/threads/{thread['id']}", headers=_h()).json()["telegram_targets"] == []
    assert _targets(env, thread["id"], ["222", "111", "222"]).json()["telegram_targets"] == ["222", "111"]
    assert _targets(env, thread["id"], ["x"]).status_code == 422
    assert _targets(env, thread["id"], [str(n) for n in range(11)]).status_code == 422
    other = _targets(env, thread["id"], ["111"], USER_B)  # not USER_B's thread at all
    assert other.status_code == 404
    assert env.client.patch(f"/v1/chat/threads/{thread['id']}", json={"title": "t"}, headers=_h()).json()["telegram_targets"] == ["222", "111"]
    assert _targets(env, thread["id"], []).json()["telegram_targets"] == []


def test_a_thread_reads_with_empty_targets_by_default(env):
    thread = env.thread()
    assert thread["telegram_targets"] == []
    assert env.client.get(f"/v1/chat/threads/{thread['id']}", headers=_h()).json()["telegram_targets"] == []
