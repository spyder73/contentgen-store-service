"""A clip uses only media its caller may use: their own, or a row with no owner.

Before this, swap took any new_media_id and GET /v1/clips/{id}/full loaded media
by id with no owner filter: a caller who knew another user's media id could put
it on a clip of their own and read the row (prompt, file_url, metadata) back.
Now swap answers another user's media as a missing media id, and /full returns
only the media the caller may use.

PUT and PATCH do not check the ids a clip names, on purpose: the backend writes a
clip's refs while a placeholder item is still generating and its row not yet
saved. So another user's id has to get exactly the answer a missing id gets, or
the answer tells a caller which ids exist; the read is where it is held back.
Media ids are compared in one canonical form, however they were written. No
header is an internal job: unfiltered. Shares its sqlite fixture with
tests/test_clips_ownership.py.
"""
from __future__ import annotations

import json
import uuid

import pytest

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


def _full_ids(env: Env, user: str | None, cid: str) -> list[str]:
    r = env.client.get(f"/v1/clips/{cid}/full", headers=_headers(user))
    assert r.status_code == 200, r.text
    return [m["id"] for m in r.json()["media"]]


def _shape(response, *ids: str) -> tuple:
    """What an answer says, with the ids the caller named and the clock left out."""
    body = response.text
    for media_id in ids:
        body = body.replace(media_id, "<id>")
    data = json.loads(body)
    data.pop("created_at", None), data.pop("updated_at", None)
    return response.status_code, data


def _forms(media_id: str) -> dict[str, str]:
    """The ways uuid.UUID reads one id, other than its canonical spelling."""
    return {
        "upper": media_id.upper(),
        "braces": "{" + media_id + "}",
        "urn": "urn:uuid:" + media_id,
        "no hyphens": media_id.replace("-", ""),
    }


FORMS = list(_forms(str(uuid.uuid4())))


# ── swap ─────────────────────────────────────────────────────────────────────


def test_swapping_in_another_users_media_is_a_missing_media_id(env):
    cid = env.clip(USER_B)
    old = env.images(cid)[0]
    theirs, missing = env.new_media(USER_A), str(uuid.uuid4())

    foreign = _swap(env, USER_B, cid, old, theirs)
    absent = _swap(env, USER_B, cid, old, missing)

    assert (foreign.status_code, absent.status_code) == (404, 404)
    # The same answer byte for byte, bar the id the caller itself named.
    assert foreign.text.replace(theirs, "<id>") == absent.text.replace(missing, "<id>")
    assert foreign.json()["detail"].startswith("media_not_found")
    assert env.images(cid) == [old] and env.row(cid).is_dirty is False


@pytest.mark.parametrize("form", FORMS)
def test_swap_reads_a_media_id_in_any_spelling(env, form):
    cid = env.clip(USER_B)
    old = env.images(cid)[0]
    theirs, mine = env.new_media(USER_A), env.new_media(USER_B)

    assert _swap(env, USER_B, cid, old, _forms(theirs)[form]).status_code == 404
    assert env.images(cid) == [old]
    # Own media in that spelling works, and is stored in the canonical one.
    assert _swap(env, USER_B, cid, old, _forms(mine)[form]).status_code == 200
    assert env.images(cid) == [mine]


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


# ── PUT and PATCH: another user's id is a missing id ─────────────────────────


def test_put_and_patch_answer_another_users_media_id_exactly_as_a_missing_one(env):
    theirs, missing = env.new_media(USER_A), str(uuid.uuid4())
    for write in (
        lambda cid, ref: _put(env, USER_B, cid, name="Same", media_refs=_refs(ref)),
        lambda cid, ref: _patch(env, USER_B, cid, name="Same", media_refs=_refs(ref)),
    ):
        foreign_clip, missing_clip = env.clip(USER_B), env.clip(USER_B)

        foreign, absent = write(foreign_clip, theirs), write(missing_clip, missing)

        assert foreign.status_code == 200
        assert _shape(foreign, theirs, foreign_clip) == _shape(absent, missing, missing_clip)
        # Neither is returned on a read, so the read tells them apart no better.
        assert _full_ids(env, USER_B, foreign_clip) == _full_ids(env, USER_B, missing_clip) == []


@pytest.mark.parametrize("form", FORMS)
def test_put_and_patch_store_media_ids_in_canonical_form(env, form):
    mine, theirs = env.new_media(USER_B), env.new_media(USER_A)
    cid = env.clip(USER_B)

    assert _put(env, USER_B, cid, media_refs=_refs(_forms(mine)[form], _forms(theirs)[form])).status_code == 200
    assert env.images(cid) == [mine, theirs]
    assert _full_ids(env, USER_B, cid) == [mine]  # the foreign one in any spelling is held back

    assert _patch(env, USER_B, cid, media_refs=_refs(_forms(theirs)[form], _forms(mine)[form])).status_code == 200
    assert env.images(cid) == [theirs, mine]
    assert _full_ids(env, USER_B, cid) == [mine]


def test_put_and_patch_still_take_refs_that_are_not_saved_or_not_ids(env):
    mine, system = env.new_media(USER_B), env.new_media(None)
    unsaved, malformed = str(uuid.uuid4()), "not-a-uuid"  # a placeholder item's row is not saved yet
    cid = env.clip(USER_B)

    assert _put(env, USER_B, cid, media_refs=_refs(mine, system, unsaved, malformed)).status_code == 200
    assert env.images(cid) == [mine, system, unsaved, malformed]
    assert _patch(env, USER_B, cid, media_refs=_refs(unsaved, mine)).status_code == 200
    assert env.images(cid) == [unsaved, mine]
    assert _full_ids(env, USER_B, cid) == [mine]  # a ref that is not an id costs the read nothing


def test_another_users_clip_is_still_just_not_found_whatever_media_it_names(env):
    cid = env.clip(USER_A)

    for r in (_put(env, USER_B, cid, media_refs=_refs(env.new_media(USER_A))),
              _patch(env, USER_B, cid, media_refs=_refs(env.new_media(USER_A)))):
        assert (r.status_code, r.json()) == (404, {"detail": "not_found"})


# ── GET /full ────────────────────────────────────────────────────────────────


def test_full_returns_only_media_the_caller_may_use(env):
    mine, theirs, system = env.new_media(USER_B), env.new_media(USER_A), env.new_media(None)
    cid = _clip_with(env, USER_B, mine, theirs, system, str(uuid.uuid4()))

    assert sorted(_full_ids(env, USER_B, cid)) == sorted([mine, system])
    # No caller: an internal job sees every row the clip names.
    assert sorted(_full_ids(env, None, cid)) == sorted([mine, theirs, system])
    # The ref itself is left on the clip.
    assert env.images(cid)[:3] == [mine, theirs, system]


@pytest.mark.parametrize("form", FORMS)
def test_full_holds_back_another_users_media_whatever_spelling_the_ref_has(env, form):
    mine, theirs = env.new_media(USER_B), env.new_media(USER_A)
    # An old clip, written before refs were canonical.
    cid = _clip_with(env, USER_B, _forms(mine)[form], _forms(theirs)[form])

    assert _full_ids(env, USER_B, cid) == [mine]
    assert sorted(_full_ids(env, None, cid)) == sorted([mine, theirs])
