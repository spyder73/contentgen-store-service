"""Productions keep background media jobs as watches, plus the scenes a user approved and the
tries spent per scene (phase 4a). All of it lives in the row's existing JSONB columns, so there
is no migration: a watch gains optional fields, ``artifacts`` gains ``approved_scenes`` and
``tries``, and the patch gains ``add_approved_scenes`` and ``add_tries``.

Same real-app harness as ``test_chat_productions.py``; the key strings are the Go side's and
the store treats them as opaque.
"""
from __future__ import annotations

from app.models import ChatProduction
from tests.test_chat import USER_A, USER_B, _h, env  # noqa: F401  (env is the fixture)
from tests.test_chat_productions import _get, _new, _patch

MEDIA_WATCH = {
    "kind": "media",
    "clip_id": "",
    "status": "running",
    "started_at": "2026-11-09T10:00:00.123456Z",
    "media_id": "m1",
    "batch": "b1",
    "what": "video",
    "vendor": "runware",
    "vendor_job_id": "job-1",
    "request_id": "req-1",
    "scene_key": "f:img1",
    "failure_kind": "",
    "failure": "",
}


def _media(media_id: str, status: str = "running", **over) -> dict:
    return {"kind": "media", "media_id": media_id, "status": status, "batch": "b1", "what": "video", **over}


def _scene(key: str, **over) -> dict:
    return {"key": key, "media_ids": ["m1"], "preview_id": "p1", "approved_at": "2026-11-09T10:00:00Z", **over}


def _wakes() -> str:
    return "/v1/chat/productions/pending-wakes"


# ── the watch ────────────────────────────────────────────────────────────────


def test_media_watch_round_trips_every_field(env):
    p = _new(env)
    ended = {**MEDIA_WATCH, "media_id": "m2", "status": "failed", "failure_kind": "content_filtered",
             "failure": "blocked by the safety filter", "what": "music", "vendor": "suno", "scene_key": ""}
    out = _patch(env, p["id"], watches=[MEDIA_WATCH, ended])
    assert out.status_code == 200, out.text
    assert out.json()["watches"] == [MEDIA_WATCH, ended]
    assert _get(env, p["id"])["watches"] == [MEDIA_WATCH, ended]
    # The sweep sees the stored watch, so what was written is what the match reads.
    found = env.client.get("/v1/chat/productions/watching-media?media_id=m1", headers=_h(None)).json()
    assert [x["id"] for x in found] == [p["id"]]


def test_a_watch_names_its_vendor_and_kind_of_job_or_nothing(env):
    p = _new(env)
    for bad in ({"vendor": "other"}, {"what": "image"}):
        assert _patch(env, p["id"], watches=[_media("m1", **bad)]).status_code == 422, bad
    assert _get(env, p["id"])["watches"] == []


def test_a_long_failure_is_cut_to_300_characters_never_refused(env):
    """The watches list is replaced whole: a refused end-of-job write would leave the job running."""
    p = _new(env)
    long = "".join(chr(ord("a") + i % 26) for i in range(1000))
    out = _patch(env, p["id"], watches=[_media("m1", "failed", failure=long)])
    assert out.status_code == 200, out.text
    assert out.json()["watches"][0]["failure"] == long[:300]
    assert _get(env, p["id"])["watches"][0]["failure"] == long[:300]
    # Up to 300 is kept whole; the cut is by characters, not bytes.
    assert _patch(env, p["id"], watches=[_media("m1", "failed", failure="é" * 300)]).json()["watches"][0]["failure"] == "é" * 300
    assert _patch(env, p["id"], watches=[_media("m1", "failed", failure="é" * 301)]).json()["watches"][0]["failure"] == "é" * 300


def test_render_watch_without_media_fields_still_valid(env):
    p = _new(env)
    render = {"kind": "render", "clip_id": "c1", "status": "rendering"}
    assert _patch(env, p["id"], watches=[render]).status_code == 200
    watch = _get(env, p["id"])["watches"][0]
    assert (watch["kind"], watch["clip_id"], watch["status"], watch["started_at"]) == ("render", "c1", "rendering", None)
    assert (watch["media_id"], watch["batch"], watch["what"], watch["vendor"], watch["scene_key"]) == ("", "", "", "", "")

    # A client may send back the watches exactly as it read them (empty means not set).
    assert _patch(env, p["id"], watches=_get(env, p["id"])["watches"]).status_code == 200

    # A media watch has no clip, and the clip id is empty rather than missing.
    assert _patch(env, p["id"], watches=[_media("m1")]).status_code == 200
    assert _get(env, p["id"])["watches"][0]["clip_id"] == ""

    # A row written before this change has neither the new watch fields nor the new artifacts keys.
    old = _new(env)
    env.set_row(
        ChatProduction, old["id"],
        watches=[{"kind": "render", "clip_id": "c9", "status": "rendering"}],
        artifacts={"media_ids": ["m0"], "clip_ids": ["c9"]},
    )
    row = _get(env, old["id"])
    assert row["watches"][0]["clip_id"] == "c9" and row["watches"][0]["media_id"] == ""
    assert row["artifacts"] == {"media_ids": ["m0"], "clip_ids": ["c9"], "approved_scenes": [], "tries": {}, "scenes": {}}
    found = env.client.get("/v1/chat/productions/watching?clip_id=c9", headers=_h(None)).json()
    assert [x["id"] for x in found] == [old["id"]]


# ── approved scenes and tries ────────────────────────────────────────────────


def test_add_approved_scenes_skips_a_known_key(env):
    p = _new(env)
    first = _scene("f:img1")
    out = _patch(env, p["id"], add_approved_scenes=[first, _scene("r:a,b", media_ids=["a", "b"])]).json()
    assert out["artifacts"]["approved_scenes"] == [first, _scene("r:a,b", media_ids=["a", "b"])]

    # A key already present is skipped, even when it comes with other pictures; a new one is appended.
    again = _patch(
        env, p["id"],
        add_approved_scenes=[_scene("f:img1", media_ids=["z"], preview_id="p2"), _scene("f:img2:img3")],
    ).json()
    keys = [s["key"] for s in again["artifacts"]["approved_scenes"]]
    assert keys == ["f:img1", "r:a,b", "f:img2:img3"]
    assert again["artifacts"]["approved_scenes"][0] == first

    # A key repeated inside one patch counts once.
    twice = _patch(env, p["id"], add_approved_scenes=[_scene("f:new"), _scene("f:new", preview_id="p3")]).json()
    assert [s["key"] for s in twice["artifacts"]["approved_scenes"]].count("f:new") == 1
    assert _get(env, p["id"])["artifacts"]["approved_scenes"] == twice["artifacts"]["approved_scenes"]


def test_an_approved_scene_left_without_a_time_is_stamped(env):
    p = _new(env)
    out = _patch(env, p["id"], add_approved_scenes=[{"key": "f:img1"}]).json()
    scene = out["artifacts"]["approved_scenes"][0]
    assert (scene["key"], scene["media_ids"], scene["preview_id"]) == ("f:img1", [], "")
    assert scene["approved_at"]
    assert _patch(env, p["id"], add_approved_scenes=[{"key": ""}]).status_code == 422


def test_add_tries_increments_atomically(env):
    p = _new(env)
    assert _patch(env, p["id"], add_tries={"f:img1": 1, "r:a,b": 2}).json()["artifacts"]["tries"] == {"f:img1": 1, "r:a,b": 2}
    out = _patch(env, p["id"], add_tries={"f:img1": 1, "f:img2": 1}).json()
    assert out["artifacts"]["tries"] == {"f:img1": 2, "r:a,b": 2, "f:img2": 1}
    assert _get(env, p["id"])["artifacts"]["tries"] == {"f:img1": 2, "r:a,b": 2, "f:img2": 1}


def test_add_tries_refund_floors_at_zero(env):
    p = _new(env)
    _patch(env, p["id"], add_tries={"f:img1": 1})
    assert _patch(env, p["id"], add_tries={"f:img1": -1}).json()["artifacts"]["tries"] == {"f:img1": 0}
    # A refund that is larger than what was spent, and one for a key never tried, never go below 0.
    _patch(env, p["id"], add_tries={"f:img1": 2})
    out = _patch(env, p["id"], add_tries={"f:img1": -5, "f:never": -1}).json()
    assert out["artifacts"]["tries"] == {"f:img1": 0, "f:never": 0}


def test_other_artifact_writes_keep_the_scenes_and_tries(env):
    """The artifacts column is rebuilt on every artifact write: nothing it holds may be dropped."""
    p = _new(env)
    _patch(env, p["id"], add_approved_scenes=[_scene("f:img1")], add_tries={"f:img1": 1})
    out = _patch(env, p["id"], add_media_ids=["m1", "m1"], add_clip_ids=["c1"]).json()
    assert out["artifacts"]["media_ids"] == ["m1"] and out["artifacts"]["clip_ids"] == ["c1"]
    assert [s["key"] for s in out["artifacts"]["approved_scenes"]] == ["f:img1"]
    assert out["artifacts"]["tries"] == {"f:img1": 1}
    # And the other way round: a try or a scene leaves the ids as they were.
    more = _patch(env, p["id"], add_tries={"f:img1": 1}, add_approved_scenes=[_scene("f:img2")]).json()
    assert (more["artifacts"]["media_ids"], more["artifacts"]["clip_ids"]) == (["m1"], ["c1"])
    assert more["artifacts"]["tries"] == {"f:img1": 2}


def test_closed_production_refuses_new_scenes(env):
    p = _new(env)
    _patch(env, p["id"], close={"outcome": "done"})
    for change in (
        {"add_approved_scenes": [_scene("f:img1")]},
        {"add_tries": {"f:img1": 1}},
        {"acked_seq": 0, "add_tries": {"f:img1": 1}},  # an ack does not carry another change in
    ):
        r = _patch(env, p["id"], **change)
        assert (r.status_code, r.json()) == (409, {"detail": "production_closed"}), change
    after = _get(env, p["id"])["artifacts"]
    assert (after["approved_scenes"], after["tries"]) == ([], {})


# ── the sweeps ───────────────────────────────────────────────────────────────


def test_pending_wakes_includes_running_media(env):
    """A restart cuts a media job off with nothing owed yet: the boot has to find it."""
    running = _new(env)
    _patch(env, running["id"], watches=[_media("m0", "ready"), _media("m1")])
    theirs = _new(env, user=USER_B)
    _patch(env, theirs["id"], USER_B, watches=[_media("m2")])
    ended = _new(env)
    _patch(env, ended["id"], watches=[_media("m3", "ready"), _media("m4", "failed")])
    closed = _new(env)
    _patch(env, closed["id"], watches=[_media("m5")], close={"outcome": "dropped"})
    other_kind = _new(env)
    _patch(env, other_kind["id"], watches=[{"kind": "other", "status": "running"}])
    _new(env)  # waits on nothing

    listed = env.client.get(_wakes(), headers=_h(None)).json()
    assert {x["id"] for x in listed} == {running["id"], theirs["id"]}


def test_watching_media_finds_only_running_open(env):
    a = _new(env)
    _patch(env, a["id"], watches=[_media("m0"), _media("mx")])
    b = _new(env)
    _patch(env, b["id"], watches=[_media("mx", "ready")])
    c = _new(env)
    _patch(env, c["id"], watches=[_media("my")])
    d = _new(env)
    _patch(env, d["id"], watches=[_media("mx")], close={"outcome": "dropped"})
    e = _new(env)
    _patch(env, e["id"], watches=[{"kind": "render", "clip_id": "mx", "status": "rendering", "media_id": "mx"}])
    f = _new(env, user=USER_B)
    _patch(env, f["id"], USER_B, watches=[_media("mx")])

    url = "/v1/chat/productions/watching-media"
    found = env.client.get(f"{url}?media_id=mx", headers=_h(None)).json()
    assert {x["id"] for x in found} == {a["id"], f["id"]}
    assert env.client.get(f"{url}?media_id=nope", headers=_h(None)).json() == []
    assert env.client.get(url, headers=_h(None)).status_code == 422
    assert env.client.get(f"{url}?media_id=", headers=_h(None)).status_code == 422
    # It is a fixed path, not a production id, and it needs the internal secret like watching.
    assert env.client.get(f"{url}?media_id=mx").status_code == 401
    # The two sweeps stay apart: watching finds the render, not the media jobs of the same name.
    clips = env.client.get("/v1/chat/productions/watching?clip_id=mx", headers=_h(None)).json()
    assert [x["id"] for x in clips] == [e["id"]]
