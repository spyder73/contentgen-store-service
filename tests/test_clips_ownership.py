"""The by-id clip routes answer only for the clip's owner.

Before this, GET/PATCH/DELETE/PUT /v1/clips/{id}, GET /v1/clips/{id}/full and
POST /v1/clips/{id}/swap loaded a clip by id alone: any caller who knew another
user's clip id could read it, change it, delete it or overwrite it with a PUT.
Now the caller comes from X-User-ID, and a clip owned by someone else answers
exactly as a missing one (404 not_found, clip_not_found for swap) and is left
untouched. No header is an internal job (pipeline runner, scheduler) and still
works; a legacy clip with no owner stays reachable. These tests drive the real
routes over sqlite, as tests/test_series_ownership.py does.
"""
from __future__ import annotations

import uuid

from tests.conftest import USER_A, USER_B


# ── another user's clip is a missing clip, on every route ───────────────────


def test_another_users_clip_cannot_be_read(env):
    cid = env.clip(USER_A)
    missing = str(uuid.uuid4())

    for path in (f"/v1/clips/{cid}", f"/v1/clips/{cid}/full"):
        r = env.client.get(path, headers=env.headers(USER_B))
        assert (r.status_code, r.json()) == (404, {"detail": "not_found"}), path
    # Indistinguishable from an id that does not exist.
    r = env.client.get(f"/v1/clips/{missing}", headers=env.headers(USER_B))
    assert (r.status_code, r.json()) == (404, {"detail": "not_found"})


def test_another_users_clip_cannot_be_patched(env):
    cid = env.clip(USER_A)

    r = env.client.patch(f"/v1/clips/{cid}", json={"name": "Taken"}, headers=env.headers(USER_B))

    assert (r.status_code, r.json()) == (404, {"detail": "not_found"})
    row = env.row(cid)
    assert (row.name, row.user_id, row.is_dirty) == ("Original", USER_A, False)


def test_another_users_clip_cannot_be_deleted(env):
    cid = env.clip(USER_A)

    r = env.client.delete(f"/v1/clips/{cid}", headers=env.headers(USER_B))

    assert (r.status_code, r.json()) == (404, {"detail": "not_found"})
    assert env.row(cid) is not None


def test_another_users_clip_cannot_be_swapped(env):
    cid = env.clip(USER_A)
    old = env.images(cid)[0]

    r = env.swap(USER_B, cid, old, env.new_media(USER_B))

    assert (r.status_code, r.json()) == (404, {"detail": "clip_not_found"})
    assert env.images(cid) == [old]
    assert env.row(cid).is_dirty is False


def test_another_users_clip_cannot_be_overwritten_by_a_put(env):
    cid = env.clip(USER_A)

    r = env.put(USER_B, cid, name="Taken", style={"look": "mine"})

    assert (r.status_code, r.json()) == (404, {"detail": "not_found"})
    row = env.row(cid)
    assert (row.name, row.user_id, row.style) == ("Original", USER_A, {})


# ── the owner is unaffected ──────────────────────────────────────────────────


def test_the_owner_reads_their_clip_and_sees_the_owner(env):
    cid = env.clip(USER_A)

    got = env.client.get(f"/v1/clips/{cid}", headers=env.headers(USER_A))
    full = env.client.get(f"/v1/clips/{cid}/full", headers=env.headers(USER_A))

    assert got.status_code == 200 and got.json()["user_id"] == USER_A
    assert full.status_code == 200
    assert full.json()["clip"]["user_id"] == USER_A
    assert [m["id"] for m in full.json()["media"]] == env.images(cid)


def test_the_owner_patches_swaps_puts_and_deletes_their_clip(env):
    cid = env.clip(USER_A)
    old, new = env.images(cid)[0], env.new_media(USER_A)

    r = env.client.patch(f"/v1/clips/{cid}", json={"name": "Renamed"}, headers=env.headers(USER_A))
    assert r.status_code == 200 and r.json()["name"] == "Renamed"

    r = env.swap(USER_A, cid, old, new)
    assert r.status_code == 200 and env.images(cid) == [new]

    r = env.put(USER_A, cid, name="Put")
    assert r.status_code == 200 and r.json()["user_id"] == USER_A
    assert env.row(cid).name == "Put"

    assert env.client.delete(f"/v1/clips/{cid}", headers=env.headers(USER_A)).status_code == 204
    assert env.row(cid) is None


def test_a_new_clip_put_belongs_to_the_caller(env):
    cid = str(uuid.uuid4())

    r = env.put(USER_B, cid, name="Mine")

    assert r.status_code == 200 and r.json()["user_id"] == USER_B
    assert env.row(cid).user_id == USER_B
    assert env.client.get(f"/v1/clips/{cid}", headers=env.headers(USER_A)).status_code == 404


# ── no header: an internal job, as before ────────────────────────────────────


def test_without_a_user_every_route_still_works(env):
    cid = env.clip(USER_A)
    old, new = env.images(cid)[0], env.new_media(USER_A)

    assert env.client.get(f"/v1/clips/{cid}", headers=env.headers(None)).status_code == 200
    assert env.client.get(f"/v1/clips/{cid}/full", headers=env.headers(None)).status_code == 200
    r = env.client.patch(f"/v1/clips/{cid}", json={"name": "Job"}, headers=env.headers(None))
    assert r.status_code == 200 and r.json()["name"] == "Job"
    assert env.swap(None, cid, old, new).status_code == 200
    assert env.put(None, cid, name="Job put").status_code == 200
    # An update never changes the owner.
    assert env.row(cid).user_id == USER_A
    assert env.client.delete(f"/v1/clips/{cid}", headers=env.headers(None)).status_code == 204
    assert env.row(cid) is None


def test_a_new_clip_put_without_a_user_has_no_owner(env):
    cid = str(uuid.uuid4())

    r = env.put(None, cid, name="Job")

    assert r.status_code == 200 and r.json()["user_id"] is None
    assert env.row(cid).user_id is None


# ── a legacy clip with no owner stays reachable ──────────────────────────────


def test_a_clip_with_no_owner_is_reachable_by_any_user(env):
    cid = env.clip(None)
    old, new = env.images(cid)[0], env.new_media(None)

    for user in (USER_A, USER_B):
        got = env.client.get(f"/v1/clips/{cid}", headers=env.headers(user))
        assert got.status_code == 200 and got.json()["user_id"] is None
        assert env.client.get(f"/v1/clips/{cid}/full", headers=env.headers(user)).status_code == 200
    r = env.client.patch(f"/v1/clips/{cid}", json={"name": "Adopted"}, headers=env.headers(USER_A))
    assert r.status_code == 200 and r.json()["name"] == "Adopted"
    assert env.swap(USER_B, cid, old, new).status_code == 200
    assert env.put(USER_A, cid, name="Put").status_code == 200
    assert env.client.delete(f"/v1/clips/{cid}", headers=env.headers(USER_B)).status_code == 204
    assert env.row(cid) is None
