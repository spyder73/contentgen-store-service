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


@pytest.mark.parametrize("header", ["user-1", "not a uuid", "1234", "00000000-0000-0000-0000-00000000000z"])
def test_a_header_that_is_not_a_uuid_names_no_user(env, header):
    cid = env.clip(USER_A)

    assert _as(env, header, "/v1/clips").status_code == 401  # needs a user
    assert _as(env, header, "/v1/media/stats").status_code == 401
    # Where a user is optional it is no header at all: what the route does for none.
    assert _as(env, header, f"/v1/clips/{cid}").status_code == env.client.get(
        f"/v1/clips/{cid}", headers={"X-Internal-Secret": SECRET}).status_code
