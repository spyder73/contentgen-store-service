"""A clip uses only media its caller may use: their own, or a row with no owner.

Before this, swap took any new_media_id, PUT/PATCH took any media_refs, and
GET /v1/clips/{id}/full loaded media by id with no owner filter: a caller who
knew another user's media id could attach it to a clip of their own and read the
row (prompt, file_url, metadata) back. Now swap answers another user's media as
a missing media id, PUT/PATCH refuse a body naming it (404 media_not_found), and
/full does not return it. An id with no row at all is still accepted by PUT/PATCH,
because the backend writes a clip's refs while a placeholder item is still
generating and its row not yet saved. No header is an internal job: unfiltered.
Shares its sqlite fixture with tests/test_clips_ownership.py.
"""
from __future__ import annotations

import uuid

from app.models import ClipPrompt
from tests.test_clips_ownership import (  # noqa: F401  (env is a fixture)
    USER_A,
    USER_B,
    Env,
    _headers,
    _put,
    _swap,
    env,
)


def _refs(*ids: str) -> dict:
    return {"images": list(ids), "ai_videos": [], "audios": []}


def _clip_with(env: Env, user: str | None, *ids: str) -> str:
    cid = str(uuid.uuid4())
    env.seed(ClipPrompt(id=cid, user_id=user, name="Original", media_refs=_refs(*ids)))
    return cid


def _patch(env: Env, user: str | None, cid: str, **body):
    return env.client.patch(f"/v1/clips/{cid}", json=body, headers=_headers(user))


def _full_ids(env: Env, user: str | None, cid: str) -> set[str]:
    r = env.client.get(f"/v1/clips/{cid}/full", headers=_headers(user))
    assert r.status_code == 200, r.text
    return {m["id"] for m in r.json()["media"]}


# ── swap ─────────────────────────────────────────────────────────────────────


def test_swapping_in_another_users_media_is_a_missing_media_id(env):
    cid = env.clip(USER_B)
    old = env.images(cid)[0]
    theirs, missing = env.new_media(USER_A), str(uuid.uuid4())

    foreign = _swap(env, USER_B, cid, old, theirs)
    absent = _swap(env, USER_B, cid, old, missing)

    assert (foreign.status_code, absent.status_code) == (404, 404)
    # The same answer, bar the id the caller itself named.
    assert foreign.json()["detail"] == absent.json()["detail"].replace(missing, theirs)
    assert foreign.json()["detail"].startswith("media_not_found")
    assert env.images(cid) == [old] and env.row(cid).is_dirty is False


def test_swap_takes_own_media_and_media_with_no_owner(env):
    cid = env.clip(USER_B)
    old = env.images(cid)[0]
    system = env.new_media(None)

    assert _swap(env, USER_B, cid, old, system).status_code == 200
    mine = env.new_media(USER_B)
    assert _swap(env, USER_B, cid, system, mine).status_code == 200
    assert env.images(cid) == [mine]


def test_swap_without_a_user_still_takes_any_media(env):
    cid = env.clip(USER_B)

    r = _swap(env, None, cid, env.images(cid)[0], env.new_media(USER_A))

    assert r.status_code == 200


# ── PUT and PATCH ────────────────────────────────────────────────────────────


def test_a_put_naming_another_users_media_is_refused_and_writes_nothing(env):
    theirs = env.new_media(USER_A)
    new_id = str(uuid.uuid4())
    cid = env.clip(USER_B)
    before = env.row(cid).media_refs

    r = _put(env, USER_B, cid, name="Taken", media_refs=_refs(theirs))
    assert (r.status_code, r.json()) == (404, {"detail": f"media_not_found: {theirs}"})
    assert (env.row(cid).name, env.row(cid).media_refs) == ("Original", before)

    # A clip that does not exist yet is not created either.
    assert _put(env, USER_B, new_id, media_refs=_refs(theirs)).status_code == 404
    assert env.row(new_id) is None


def test_a_patch_naming_another_users_media_is_refused_and_writes_nothing(env):
    theirs = env.new_media(USER_A)
    cid = env.clip(USER_B)
    before = env.row(cid).media_refs

    r = _patch(env, USER_B, cid, name="Taken", media_refs=_refs(theirs))

    assert (r.status_code, r.json()) == (404, {"detail": f"media_not_found: {theirs}"})
    assert (env.row(cid).name, env.row(cid).media_refs) == ("Original", before)


def test_put_and_patch_still_take_refs_the_caller_may_use(env):
    mine, system = env.new_media(USER_B), env.new_media(None)
    unsaved, malformed = str(uuid.uuid4()), "not-a-uuid"  # a placeholder item's row is not saved yet
    cid = env.clip(USER_B)

    assert _put(env, USER_B, cid, media_refs=_refs(mine, system, unsaved, malformed)).status_code == 200
    assert env.images(cid) == [mine, system, unsaved, malformed]
    assert _patch(env, USER_B, cid, media_refs=_refs(unsaved, mine)).status_code == 200
    assert env.images(cid) == [unsaved, mine]


def test_a_patch_that_leaves_the_refs_alone_works_on_a_clip_already_holding_foreign_ones(env):
    cid = _clip_with(env, USER_B, env.new_media(USER_A))

    r = _patch(env, USER_B, cid, name="Renamed")

    assert r.status_code == 200 and env.row(cid).name == "Renamed"


def test_without_a_user_put_and_patch_take_any_refs(env):
    cid = env.clip(USER_B)
    theirs = env.new_media(USER_A)

    assert _put(env, None, cid, media_refs=_refs(theirs)).status_code == 200
    assert _patch(env, None, cid, media_refs=_refs(theirs)).status_code == 200


def test_another_users_clip_is_still_just_not_found_whatever_media_it_names(env):
    cid = env.clip(USER_A)

    for r in (_put(env, USER_B, cid, media_refs=_refs(env.new_media(USER_A))),
              _patch(env, USER_B, cid, media_refs=_refs(env.new_media(USER_A)))):
        assert (r.status_code, r.json()) == (404, {"detail": "not_found"})


# ── GET /full ────────────────────────────────────────────────────────────────


def test_full_returns_only_media_the_caller_may_use(env):
    mine, theirs, system = env.new_media(USER_B), env.new_media(USER_A), env.new_media(None)
    cid = _clip_with(env, USER_B, mine, theirs, system, str(uuid.uuid4()))

    assert _full_ids(env, USER_B, cid) == {mine, system}
    # No caller: an internal job sees every row the clip names.
    assert _full_ids(env, None, cid) == {mine, theirs, system}
    # The ref itself is left on the clip.
    assert env.images(cid)[:3] == [mine, theirs, system]
