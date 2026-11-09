"""A media row's file_url is validated where it is written.

The Go backend turns a row's file_url into local file operations (read, rename,
remove), so a row's owner must not be able to point it at an arbitrary path:
PUT and PATCH of /v1/media/{id} answer 422 invalid_file_url, without echoing the
value, for anything but empty, one flat file of /media/uploads/ or /assets/, or an
http(s) URL with a host. Over sqlite, driving the real routes (see conftest.py).
"""
from __future__ import annotations

import uuid

import pytest

from tests.conftest import USER_A, USER_B

GOOD = [
    "",
    "/assets/_waiting.png",
    "/assets/_failed.png",
    f"/media/uploads/{uuid.uuid4()}.png",
    "/media/uploads/my renamed clip.mp4",
    f"/assets/{uuid.uuid4()}.jpg",
    "https://cdn.example.com/a/b.png?x=1",
    "http://localhost:1323/media/uploads/a.png",
]
BAD = [
    "//app/media/uploads/victim.mp4", "//etc/passwd", "/etc/passwd", "/app/media/uploads/a.png", "../x.png", "etc/passwd",
    "media/uploads/a.png", "assets/a.png", "./assets/a.png",
    "/media/uploads/../../etc/passwd", "/media/uploads/a/b.png", "/media/uploads/", "/media/uploads/..", "/assets/../x", "/assets/",
    "/assets/sub/x.png", "/media/proxy/a.mp4", "/media/uploads/a\\b.png", "/media/uploads/a\x01.png",
    "file:///etc/passwd", "ftp://example.com/a.png", "data:image/png;base64,AAAA", "javascript:alert(1)", "https://", "http:///nohost",
    " /media/uploads/a.png", "/media/uploads/a.png ",
]


def _put(env, mid, file_url):
    body = {"id": mid, "type": "image", "prompt": "p", "file_url": file_url}
    return env.client.put(f"/v1/media/{mid}", json=body, headers=env.headers(USER_A))


def _patch(env, mid, file_url):
    return env.client.patch(f"/v1/media/{mid}", json={"file_url": file_url}, headers=env.headers(USER_A))


@pytest.mark.parametrize("file_url", BAD)
def test_a_bad_file_url_is_refused_on_put_and_patch_and_the_row_is_unchanged(env, file_url):
    mid = env.new_media(USER_A, file_url="/media/uploads/original.png")

    for r in (_put(env, mid, file_url), _patch(env, mid, file_url)):
        assert (r.status_code, r.json()) == (422, {"detail": "invalid_file_url"})
        assert file_url.strip() not in r.text or not file_url.strip()  # the value is not echoed
    assert env.media_row(mid).file_url == "/media/uploads/original.png"

    new_id = str(uuid.uuid4())
    assert _put(env, new_id, file_url).status_code == 422
    assert env.media_row(new_id) is None


@pytest.mark.parametrize("file_url", GOOD)
def test_every_shape_the_writers_use_is_accepted(env, file_url):
    mid = env.new_media(USER_A, file_url="/media/uploads/original.png")

    assert _put(env, mid, file_url).status_code == 200
    assert env.media_row(mid).file_url == file_url
    assert _patch(env, mid, file_url).status_code == 200
    assert env.media_row(mid).file_url == file_url


def test_a_patch_that_does_not_set_a_file_url_is_not_checked(env):
    mid = env.new_media(USER_A, file_url="/media/uploads/original.png")

    r = env.client.patch(f"/v1/media/{mid}", json={"file_url": None}, headers=env.headers(USER_A))

    assert r.status_code == 200
    assert env.media_row(mid).file_url == "/media/uploads/original.png"


# ── a row that already holds an odd value ────────────────────────────────────

LEGACY = "/etc/legacy-odd-value"


def test_an_unchanged_odd_value_can_be_resent_so_the_rest_of_the_row_still_saves(env):
    mid = env.new_media(USER_A, file_url=LEGACY, prompt="before")

    put = env.client.put(f"/v1/media/{mid}", json={"id": mid, "type": "image", "prompt": "after", "file_url": LEGACY}, headers=env.headers(USER_A))
    patch = _patch(env, mid, LEGACY)

    assert (put.status_code, patch.status_code) == (200, 200)
    assert env.media_row(mid).prompt == "after" and env.media_row(mid).file_url == LEGACY


def test_changing_a_row_to_a_bad_value_is_still_refused(env):
    mid = env.new_media(USER_A, file_url=LEGACY)

    for r in (_put(env, mid, "/etc/other-odd-value"), _patch(env, mid, "//app/x")):
        assert (r.status_code, r.json()) == (422, {"detail": "invalid_file_url"})
    assert env.media_row(mid).file_url == LEGACY

    # ...and an odd row can still be changed to a good value.
    assert _patch(env, mid, "/media/uploads/fixed.png").status_code == 200


def test_a_new_row_with_a_bad_value_is_still_refused(env):
    mid = str(uuid.uuid4())

    assert _put(env, mid, LEGACY).status_code == 422
    assert env.media_row(mid) is None


def test_another_users_stored_value_does_not_make_a_value_pass(env):
    mid = env.new_media(USER_B, file_url=LEGACY)

    for r in (_put(env, mid, LEGACY), _patch(env, mid, LEGACY)):
        assert (r.status_code, r.json()) == (422, {"detail": "invalid_file_url"})
    assert env.media_row(mid).user_id == USER_B and env.media_row(mid).file_url == LEGACY
