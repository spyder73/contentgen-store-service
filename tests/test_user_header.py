"""X-User-ID is read in one canonical form.

Real user ids are uuids (users.id, so the JWT's subject), and routes compare them
as strings against the owner a row names. A header spelled another way (uppercase,
braces, no hyphens, padded) used to name a user who owned nothing; it now names the
same user. A header that is not a uuid names no valid user: it is treated as no
header, so a route that needs a user answers 401.
"""
from __future__ import annotations

import pytest

from tests.conftest import SECRET, USER_A, USER_B


def _as(env, header, path="/v1/clips"):
    return env.client.get(path, headers={"X-Internal-Secret": SECRET, "X-User-ID": header})


SPELLINGS = {
    "upper": lambda u: u.upper(),
    "braces": lambda u: "{" + u + "}",
    "no hyphens": lambda u: u.replace("-", ""),
    "padded": lambda u: f"  {u} ",
}


@pytest.mark.parametrize("spelling", SPELLINGS)
def test_a_user_in_another_spelling_is_the_same_user(env, spelling):
    cid = env.clip(USER_A)
    header = SPELLINGS[spelling](USER_A)

    assert _as(env, header, f"/v1/clips/{cid}").status_code == 200
    assert _as(env, header, "/v1/clips").json()["total"] == 1  # a listing is that user's
    # ...and another user is still another user, however they are spelled.
    assert _as(env, SPELLINGS[spelling](USER_B), f"/v1/clips/{cid}").status_code == 404


INVALID = ["user-1", "not a uuid", "1234", "00000000-0000-0000-0000-00000000000z"]


@pytest.mark.parametrize("header", INVALID)
def test_a_header_that_is_not_a_uuid_is_refused_on_every_route(env, header):
    cid, mid = env.clip(USER_A), env.new_media(USER_A)
    h = {"X-Internal-Secret": SECRET, "X-User-ID": header}
    calls = [
        env.client.get(f"/v1/clips/{cid}", headers=h),  # a user is optional here
        env.client.get(f"/v1/clips/{cid}/full", headers=h),
        env.client.patch(f"/v1/clips/{cid}", json={"name": "x"}, headers=h),
        env.client.delete(f"/v1/clips/{cid}", headers=h),
        env.client.get("/v1/clips", headers=h),  # a listing
        env.client.get(f"/v1/media/{mid}", headers=h),
        env.client.get(f"/v1/media/{mid}/file", headers=h),  # the byte routes take no user, or one
        env.client.put(f"/v1/media/{mid}/proxy", content=b"p", headers=h),
        env.client.get("/v1/media/stats", headers=h),
    ]

    for r in calls:
        assert (r.status_code, r.json()) == (401, {"detail": "invalid_user_id"}), r.request.url
    # Nothing was read or written on the way.
    assert env.row(cid) is not None and env.row(cid).name == "Original"
    assert env.media_row(mid).proxy_bytes is None


@pytest.mark.parametrize("header", ["", "   "])
def test_an_empty_header_is_still_no_header(env, header):
    cid = env.clip(USER_A)
    r = env.client.get(f"/v1/clips/{cid}", headers={"X-Internal-Secret": SECRET, "X-User-ID": header})

    assert r.status_code == env.client.get(f"/v1/clips/{cid}", headers={"X-Internal-Secret": SECRET}).status_code == 200
