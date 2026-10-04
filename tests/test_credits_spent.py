"""POST /v1/users/{id}/credits/spent: what a user paid, per scope of ledger keys.

Reuses the per-hold release suite's real-ORM sqlite ledger (same tables, same
append-only guards) and drives settle/release through the store's own code,
so the sums are read off exactly the rows production writes.
"""
from __future__ import annotations

import pytest

from app.db import get_session
from app.fastapi_app import create_fastapi_app
from app.stores import credits_spent
from tests.test_credits_hold_release import OTHER, SECRET, USER, _run, httpx, ledger  # noqa: F401

# usd_to_credits: ceil(usd * 100 * 1.7)
VEO_SCENE_USD = "0.40"   # 8 s Veo 3.1 Lite 720p with audio -> 68 credits
IMAGE_USD = "0.05"       # -> 9 credits (8.5 rounded up)


@pytest.mark.asyncio
async def test_sums_debits_of_every_key_in_the_scope(ledger):
    root, scene_a, scene_b, clip = _run(), _run(), _run(), _run()
    # Pipeline: the root run's image, two scene child runs' Veo videos.
    await ledger.reserve(root, "char-ref", 20)
    await ledger.settle(root, "char-ref", IMAGE_USD)
    await ledger.reserve(scene_a, "scene-video", 80)
    await ledger.settle(scene_a, "scene-video", "0")      # no telemetry: settles at the hold
    await ledger.reserve(scene_b, "scene-video", 80)
    await ledger.settle(scene_b, "scene-video", VEO_SCENE_USD)
    # Clip side: a Generate Clip video and a regenerated take, keyed by clip.
    await ledger.reserve(clip, "clip_new:req-1", 70)
    await ledger.settle(clip, "clip_new:req-1", VEO_SCENE_USD)
    await ledger.reserve(clip, "media_regenerate:req-2", 70)
    await ledger.settle(clip, "media_regenerate:req-2", VEO_SCENE_USD)
    # Not spent: an open hold, and a released one.
    await ledger.reserve(clip, "clip_new:req-3", 70)
    await ledger.reserve(clip, "clip_new:req-4", 70)
    await ledger.release(clip, "failed", key="rel-4", cp="clip_new:req-4", attempt=1)
    # Somebody else's debit on the same key never counts.
    await ledger.reserve(clip, "clip_new:req-x", 70, user=OTHER)
    await ledger.settle(clip, "clip_new:req-x", VEO_SCENE_USD, user=OTHER)

    async with ledger.factory() as s:
        out = await credits_spent.spent_by_scope(s, USER, {
            "clip": [clip, root, scene_a, scene_b],
            "run-only": [root],
            "nothing": [_run()],
        })
    # 9 (image) + 80 (hold-settled scene) + 68 (scene) + 68 + 68 (clip side)
    assert out["clip"]["credits"] == 9 + 80 + 68 + 68 + 68
    assert out["clip"]["debit_count"] == 5
    assert out["run-only"] == {"credits": 9, "usd": 0.05, "debit_count": 1}
    assert out["nothing"] == {"credits": 0, "usd": 0.0, "debit_count": 0}


@pytest.mark.asyncio
async def test_malformed_keys_are_skipped_not_fatal(ledger):
    run = _run()
    await ledger.reserve(run, "gen", 20)
    await ledger.settle(run, "gen", IMAGE_USD)
    async with ledger.factory() as s:
        out = await credits_spent.spent_by_scope(s, USER, {"c": ["not-a-uuid", "", run.upper()]})
    assert out["c"]["credits"] == 9


@pytest.mark.asyncio
async def test_route_is_owner_only_and_bounded(ledger, monkeypatch):
    monkeypatch.setenv("INTERNAL_API_SECRET", SECRET)
    app = create_fastapi_app()

    async def _session():
        async with ledger.factory() as s:
            yield s

    app.dependency_overrides[get_session] = _session
    run = _run()
    await ledger.reserve(run, "gen", 20)
    await ledger.settle(run, "gen", IMAGE_USD)
    url = f"/v1/users/{USER}/credits/spent"
    headers = {"X-User-ID": USER, "X-Internal-Secret": SECRET}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://store") as c:
        ok = await c.post(url, headers=headers, json={"scopes": {"clip-1": [run]}})
        assert ok.status_code == 200
        assert ok.json() == {"scopes": {"clip-1": {"credits": 9, "usd": 0.05, "debit_count": 1}}}

        other = await c.post(url, headers={**headers, "X-User-ID": OTHER}, json={"scopes": {"c": [run]}})
        assert other.status_code == 403

        too_many = await c.post(url, headers=headers, json={"scopes": {"c": [run] * 65}})
        assert too_many.status_code == 422
