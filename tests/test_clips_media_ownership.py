"""A clip uses only media its owner may use: their own, or a row with no owner.

Before this, swap took any new_media_id, PUT/PATCH took any media_refs and
GET /v1/clips/{id}/full loaded media by id with no owner filter: a caller who knew
another user's media id could put it on a clip of their own and read the row
(prompt, file_url, metadata) back, and so could any internal reader that sends no
header. Now every read keyed off a clip filters by the CLIP's owner, header or
not (a clip with no owner falls back to the caller, and with no caller either is
unfiltered); swap answers media the owner may not use as a missing media id; and
PUT/PATCH drop such ids before saving. An id with no media row is kept: the
backend writes a clip's refs while a placeholder item is still generating. The
answer to a write is built from what was submitted, so it is the same whether an
id was dropped or kept. Media ids are compared in one canonical form.
Shares its sqlite fixture with tests/test_clips_ownership.py.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import pytest

from app.models import ClipPrompt
from tests.conftest import USER_A, USER_B, Env


def _refs(*ids: str) -> dict:
    return {"images": list(ids), "ai_videos": [], "audios": []}


def _clip_with(env: Env, user: str | None, *ids: str) -> str:
    cid = str(uuid.uuid4())
    env.seed(ClipPrompt(id=cid, user_id=user, name="Original", media_refs=_refs(*ids)))
    return cid


def _full(env: Env, user: str | None, cid: str) -> list[str]:
    r = env.client.get(f"/v1/clips/{cid}/full", headers=env.headers(user))
    assert r.status_code == 200, r.text
    return sorted(m["id"] for m in r.json()["media"])


def _shape(response, *ids: str) -> tuple:
    """What an answer says, with the ids named in it and the clock left out."""
    body = response.text
    for media_id in ids:
        body = body.replace(media_id, "<id>")
    data = json.loads(body)
    data.pop("created_at", None), data.pop("updated_at", None)
    return response.status_code, data


def _forms(media_id: str) -> dict[str, str]:
    """The ways uuid.UUID reads one id, other than its canonical spelling."""
    return {"upper": media_id.upper(), "braces": "{" + media_id + "}", "urn": "urn:uuid:" + media_id,
            "no hyphens": media_id.replace("-", "")}


FORMS = list(_forms(str(uuid.uuid4())))


# ── swap ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("caller", [USER_B, None])
def test_swapping_in_media_the_clip_owner_may_not_use_is_a_missing_media_id(env, caller):
    cid = env.clip(USER_B)
    old = env.images(cid)[0]
    theirs, missing = env.new_media(USER_A), str(uuid.uuid4())

    foreign, absent = env.swap(caller, cid, old, theirs), env.swap(caller, cid, old, missing)

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

    assert env.swap(USER_B, cid, old, _forms(theirs)[form]).status_code == 404
    assert env.images(cid) == [old]
    assert env.swap(USER_B, cid, old, _forms(mine)[form]).status_code == 200
    assert env.images(cid) == [mine]  # stored canonical


def test_swap_takes_the_owners_media_and_media_with_no_owner(env):
    cid = env.clip(USER_B)
    system, mine = env.new_media(None), env.new_media(USER_B)

    assert env.swap(USER_B, cid, env.images(cid)[0], system).status_code == 200
    assert env.swap(None, cid, system, mine).status_code == 200
    assert env.images(cid) == [mine]


def test_a_clip_with_no_owner_uses_the_callers_media_or_anyones_without_a_caller(env):
    cid = env.clip(None)
    old = env.images(cid)[0]
    theirs = env.new_media(USER_A)

    assert env.swap(USER_B, cid, old, theirs).status_code == 404
    assert env.swap(USER_B, cid, old, env.new_media(USER_B)).status_code == 200
    assert env.swap(None, cid, env.images(cid)[0], theirs).status_code == 200


# ── PUT and PATCH: a foreign id is dropped, a missing id is kept ─────────────


def _writes(env: Env, user: str | None):
    """PUT and PATCH of refs onto a fresh clip owned by USER_B; each returns (clip id, answer)."""
    def put(refs):
        cid = env.clip(USER_B)
        return cid, env.put(user, cid, name="Same", media_refs=refs)

    def patch(refs):
        cid = env.clip(USER_B)
        return cid, env.patch(user, cid, name="Same", media_refs=refs)

    return put, patch


@pytest.mark.parametrize("caller", [USER_B, None])
def test_a_foreign_id_is_dropped_on_write_and_a_missing_one_is_kept(env, caller):
    mine, theirs, missing = env.new_media(USER_B), env.new_media(USER_A), str(uuid.uuid4())

    for write in _writes(env, caller):
        cid, r = write(_refs(mine, theirs, missing))
        assert r.status_code == 200
        assert env.images(cid) == [mine, missing]


def test_a_new_clip_put_drops_media_the_caller_may_not_use(env):
    theirs, missing, cid = env.new_media(USER_A), str(uuid.uuid4()), str(uuid.uuid4())

    assert env.put(USER_B, cid, media_refs=_refs(theirs, missing)).status_code == 200
    assert env.images(cid) == [missing]


def test_the_answer_to_a_write_is_the_same_for_a_foreign_and_a_missing_id(env):
    theirs, missing = env.new_media(USER_A), str(uuid.uuid4())
    for write in _writes(env, USER_B):
        (foreign_clip, foreign), (missing_clip, absent) = write(_refs(theirs)), write(_refs(missing))

        assert foreign.status_code == 200
        assert _shape(foreign, theirs, foreign_clip) == _shape(absent, missing, missing_clip)


def test_an_attacker_cannot_reach_media_through_their_own_clip(env):
    theirs, mine = env.new_media(USER_A), env.new_media(USER_B)
    cid = env.clip(USER_B)

    assert env.put(USER_B, cid, media_refs=_refs(mine, theirs)).status_code == 200

    assert _full(env, None, cid) == [mine]  # an internal reader, no header
    assert _full(env, USER_B, cid) == [mine]


@pytest.mark.parametrize("form", FORMS)
def test_media_ids_are_dropped_and_stored_in_canonical_form(env, form):
    mine, theirs = env.new_media(USER_B), env.new_media(USER_A)
    put, patch = _writes(env, USER_B)

    for write in (put, patch):
        cid, r = write(_refs(_forms(mine)[form], _forms(theirs)[form]))
        assert r.status_code == 200
        assert env.images(cid) == [mine]


def test_refs_that_are_not_ids_are_kept_and_cost_the_read_nothing(env):
    mine, unsaved, malformed = env.new_media(USER_B), str(uuid.uuid4()), "not-a-uuid"
    cid = env.clip(USER_B)

    assert env.put(USER_B, cid, media_refs=_refs(mine, unsaved, malformed)).status_code == 200
    assert env.images(cid) == [mine, unsaved, malformed]
    assert _full(env, USER_B, cid) == [mine]


def test_a_clip_with_no_owner_drops_what_the_caller_may_not_use_or_nothing_without_one(env):
    theirs, mine = env.new_media(USER_A), env.new_media(USER_B)
    cid = env.clip(None)

    assert env.patch(USER_B, cid, media_refs=_refs(mine, theirs)).status_code == 200
    assert env.images(cid) == [mine]
    assert env.patch(None, cid, media_refs=_refs(mine, theirs)).status_code == 200
    assert env.images(cid) == [mine, theirs]


def test_a_patch_without_refs_leaves_them_alone_and_another_users_clip_is_not_found(env):
    cid = _clip_with(env, USER_B, env.new_media(USER_A))
    before = env.images(cid)

    assert env.patch(USER_B, cid, name="Renamed").status_code == 200
    assert env.images(cid) == before

    theirs = env.clip(USER_A)
    for r in (env.put(USER_B, theirs, media_refs=_refs()), env.patch(USER_B, theirs, media_refs=_refs())):
        assert (r.status_code, r.json()) == (404, {"detail": "not_found"})


# ── GET /full ────────────────────────────────────────────────────────────────


def test_full_returns_only_media_the_clip_owner_may_use_header_or_not(env):
    mine, theirs, system = env.new_media(USER_B), env.new_media(USER_A), env.new_media(None)
    # An old clip, from before refs were dropped on write.
    cid = _clip_with(env, USER_B, mine, theirs, system, str(uuid.uuid4()))

    assert _full(env, USER_B, cid) == sorted([mine, system])
    assert _full(env, None, cid) == sorted([mine, system])  # not the victim's media
    assert env.images(cid)[:3] == [mine, theirs, system]  # the ref itself is left


def test_full_of_a_clip_with_no_owner_filters_by_the_caller_or_not_at_all(env):
    mine, theirs, system = env.new_media(USER_B), env.new_media(USER_A), env.new_media(None)
    cid = _clip_with(env, None, mine, theirs, system)

    assert _full(env, USER_B, cid) == sorted([mine, system])
    assert _full(env, None, cid) == sorted([mine, theirs, system])


@pytest.mark.parametrize("form", FORMS)
def test_full_holds_back_foreign_media_whatever_spelling_the_ref_has(env, form):
    mine, theirs = env.new_media(USER_B), env.new_media(USER_A)
    cid = _clip_with(env, USER_B, _forms(mine)[form], _forms(theirs)[form])

    assert _full(env, USER_B, cid) == [mine]
    assert _full(env, None, cid) == [mine]


# ── updated_at must not tell a dropped id from a kept one ────────────────────


def test_a_write_moves_updated_at_whether_or_not_an_id_was_dropped(env):
    mine, theirs, missing = env.new_media(USER_B), env.new_media(USER_A), str(uuid.uuid4())
    old = datetime(2020, 1, 1, tzinfo=timezone.utc)
    writes = (
        lambda cid, refs: env.put(USER_B, cid, name="Original", media_refs=refs),
        lambda cid, refs: env.patch(USER_B, cid, media_refs=refs),
    )
    for write in writes:
        for refs in (_refs(mine, theirs), _refs(mine, missing)):  # the first leaves the stored refs as they were
            cid = str(uuid.uuid4())
            env.seed(ClipPrompt(id=cid, user_id=USER_B, name="Original", media_refs=_refs(mine), created_at=old, updated_at=old))

            r = write(cid, refs)

            assert r.status_code == 200
            assert datetime.fromisoformat(r.json()["updated_at"]).year > 2020
            assert env.row(cid).updated_at.year > 2020
