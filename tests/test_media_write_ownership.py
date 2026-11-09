"""A media write by id changes only a row its caller may write: their own, or one with no owner.

PUT /v1/media/{id} used to be an upsert whose conflict branch rewrote any row that
was not frozen and set its user_id to the caller: a caller who named another user's
media id (the backend takes one from a pipeline attachment) took the row over, and
one who named a row with no owner claimed it. Now another user's row answers as a
missing one (404 not_found, no id in it) and is left untouched, and no write ever
changes a row's owner: a row with no owner keeps none. A new row is the caller's.
The other by-id write routes already behaved so; they are held to it here.
Over sqlite, driving the real routes (see conftest.py).
"""
from __future__ import annotations

import uuid

import pytest

from tests.conftest import USER_A, USER_B

ORIGINAL = dict(prompt="original", file_url="/media/uploads/original.png", name="orig", metadata_={"k": "v"})
COLUMNS = ("user_id", "prompt", "file_url", "name", "is_favourite", "metadata_", "file_data", "proxy_bytes")


def _put_row(env, user, mid):
    body = {"id": mid, "type": "image", "prompt": "changed", "file_url": "/media/uploads/changed.png", "metadata": {"k": "x"}}
    return env.client.put(f"/v1/media/{mid}", json=body, headers=env.headers(user))


def _patch_row(env, user, mid):
    return env.client.patch(f"/v1/media/{mid}", json={"file_url": "/media/uploads/changed.png"}, headers=env.headers(user))


def _put_file(env, user, mid):
    return env.client.put(f"/v1/media/{mid}/file", content=b"new bytes", headers={**env.headers(user), "Content-Type": "application/octet-stream"})


def _put_proxy(env, user, mid):
    return env.client.put(f"/v1/media/{mid}/proxy", content=b"proxy", headers={**env.headers(user), "Content-Type": "video/mp4"})


def _delete(env, user, mid):
    return env.client.delete(f"/v1/media/{mid}", headers=env.headers(user))


def _favourite(env, user, mid):
    return env.client.patch(f"/v1/media/{mid}/favourite", json={"is_favourite": True}, headers=env.headers(user))


def _rename(env, user, mid):
    return env.client.put(f"/v1/media/{mid}/rename", json={"name": "changed"}, headers=env.headers(user))


# route -> (call, status when it works)
ROUTES = {
    "PUT /v1/media/{id}": (_put_row, 200),
    "PATCH /v1/media/{id}": (_patch_row, 200),
    "PUT /v1/media/{id}/file": (_put_file, 204),
    "PUT /v1/media/{id}/proxy": (_put_proxy, 204),
    "DELETE /v1/media/{id}": (_delete, 204),
    "PATCH /v1/media/{id}/favourite": (_favourite, 200),
    "PUT /v1/media/{id}/rename": (_rename, 200),
}
ROUTE_IDS = list(ROUTES)


def _snapshot(env, mid):
    row = env.media_row(mid)
    return None if row is None else {c: getattr(row, c) for c in COLUMNS}


@pytest.mark.parametrize("route", ROUTE_IDS)
def test_another_users_media_row_is_not_found_and_left_untouched(env, route):
    call, _ = ROUTES[route]
    mid = env.new_media(USER_A, **ORIGINAL)
    before = _snapshot(env, mid)

    r = call(env, USER_B, mid)

    assert r.status_code == 404 and mid not in r.text, (r.status_code, r.text)
    assert _snapshot(env, mid) == before and before["user_id"] == USER_A


@pytest.mark.parametrize("route", ROUTE_IDS)
def test_the_owner_writes_their_row_and_stays_its_owner(env, route):
    call, ok = ROUTES[route]
    mid = env.new_media(USER_A, **ORIGINAL)
    before = _snapshot(env, mid)

    assert call(env, USER_A, mid).status_code == ok

    after = _snapshot(env, mid)
    if route.startswith("DELETE"):
        assert after is None
    else:
        assert after["user_id"] == USER_A and after != before


def test_an_upsert_by_the_owner_replaces_the_content(env):
    mid = env.new_media(USER_A, **ORIGINAL)

    assert _put_row(env, USER_A, mid).status_code == 200

    row = env.media_row(mid)
    assert (row.prompt, row.file_url, row.user_id) == ("changed", "/media/uploads/changed.png", USER_A)


@pytest.mark.parametrize("route", [r for r in ROUTE_IDS if not r.startswith("DELETE")])
def test_a_row_with_no_owner_is_written_but_never_claimed(env, route):
    call, ok = ROUTES[route]
    mid = env.new_media(None, **ORIGINAL)

    assert call(env, USER_B, mid).status_code == ok

    assert env.media_row(mid).user_id is None


def test_a_new_media_row_is_the_callers(env):
    mid = str(uuid.uuid4())

    assert _put_row(env, USER_B, mid).status_code == 200

    assert env.media_row(mid).user_id == USER_B


def test_without_a_user_an_upsert_is_still_refused_and_a_proxy_fill_still_works(env):
    mid = env.new_media(USER_A, **ORIGINAL)

    assert _put_row(env, None, mid).status_code == 401
    assert _put_proxy(env, None, mid).status_code == 204  # the backend's best-effort cache fill
    assert env.media_row(mid).user_id == USER_A
