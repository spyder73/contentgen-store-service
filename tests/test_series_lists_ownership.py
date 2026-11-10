"""GET /v1/episodes and GET /v1/characters list only the caller's series.

Before this they took no caller and listed every user's rows, or any user's by
series_id. A series with no owner (legacy) stays listed, another user's series_id is
an empty page (as a series that is not there is), and no caller is a 401 as on
GET /v1/series. The fixture and helpers are tests/test_episode_ownership.py's.
"""
from __future__ import annotations

import uuid

import pytest

from tests.test_episode_ownership import (  # noqa: F401  (env is the fixture)
    USER_A,
    USER_B,
    _character,
    _episode,
    _headers,
    _legacy_series,
    _listed,
    _series,
    env,
)


@pytest.mark.parametrize("route", ["episodes", "characters"])
def test_a_list_holds_only_the_callers_series_and_the_legacy_ones(env, route):
    make = _episode if route == "episodes" else _character
    sid_a, sid_b, legacy = _series(env, USER_A), _series(env, USER_B), _legacy_series(env)
    mine, theirs, old = make(env, USER_A, sid_a), make(env, USER_B, sid_b), make(env, USER_B, legacy)

    assert _listed(env, USER_A, f"/v1/{route}") == {mine, old}
    assert _listed(env, USER_B, f"/v1/{route}") == {theirs, old}
    assert _listed(env, USER_A, f"/v1/{route}?series_id={sid_a}") == {mine}
    assert _listed(env, USER_A, f"/v1/{route}?series_id={legacy}") == {old}


@pytest.mark.parametrize("route", ["episodes", "characters"])
def test_another_users_series_id_is_an_empty_page_not_an_error(env, route):
    make = _episode if route == "episodes" else _character
    sid_a = _series(env, USER_A)
    make(env, USER_A, sid_a)

    r = env.client.get(f"/v1/{route}?series_id={sid_a}", headers=_headers(USER_B))
    missing = env.client.get(f"/v1/{route}?series_id={uuid.uuid4()}", headers=_headers(USER_B))

    assert r.status_code == 200 and r.json() == missing.json()
    assert r.json()["items"] == [] and r.json()["total"] == 0


@pytest.mark.parametrize("route", ["series", "episodes", "characters"])
def test_a_list_with_no_caller_is_refused_as_the_series_list_is(env, route):
    sid_a = _series(env, USER_A)
    _episode(env, USER_A, sid_a)

    r = env.client.get(f"/v1/{route}", headers=_headers(None))
    assert (r.status_code, r.json()["detail"]) == (401, "user_id_required")
    r = env.client.get(f"/v1/{route}?series_id={sid_a}", headers=_headers(None))
    assert (r.status_code, r.json()["detail"]) == (401, "user_id_required")
