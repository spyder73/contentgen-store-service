"""Tests for generator profiles: routes (mocked store) + store domain logic.

Covers create v1, version bump, one-draft-per-slug 409, publish freeze (PUT
after publish -> 409), DELETE published -> 409, by-ref lookup + malformed ref,
and the character generator_profile_id round-trip.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.db import get_session
from app.fastapi_app import create_fastapi_app
from app.schemas import (
    CharacterOut,
    GeneratorProfileCreate,
    GeneratorProfileOut,
    GeneratorProfileUpdate,
)
from app.stores import generator_profiles
from app.stores.generator_profiles import GeneratorProfileError

INTERNAL_SECRET = "test-secret-xyz"


async def _mock_session():
    yield MagicMock()


@pytest.fixture(autouse=True)
def _internal_secret_env(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_SECRET", INTERNAL_SECRET)


@pytest.fixture()
def client():
    app = create_fastapi_app()
    app.dependency_overrides[get_session] = _mock_session
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


def _headers() -> dict[str, str]:
    return {"X-Internal-Secret": INTERNAL_SECRET}


def _id(n: int) -> str:
    return f"00000000-0000-0000-0000-{n:012d}"


def _spec() -> dict:
    return {
        "base_model": {"source": "hf", "id": "stabilityai/stable-diffusion-xl-base-1.0"},
        "adapters": [{"artifact_id": "artifact_ab12", "weight": 0.75}],
        "prompt": {"base_prompt": "a fox", "negative_prompt": "blurry"},
        "params": {"width": 1024, "height": 1024, "steps": 30, "cfg_scale": 7.0, "scheduler": ""},
    }


def _profile_out(id_: str, *, slug="strawberina-sdxl", version=1, status="draft", **overrides) -> GeneratorProfileOut:
    now = datetime.now(timezone.utc)
    data = {
        "id": id_,
        "slug": slug,
        "version": version,
        "status": status,
        "user_id": None,
        "name": "Strawberina SDXL",
        "media_type": "image",
        "spec": _spec(),
        "created_at": now,
        "updated_at": now,
    }
    data.update(overrides)
    return GeneratorProfileOut(**data)


# ── route-level tests (store fn patched) ──────────────────────────────────


class TestGeneratorProfileRoutes:
    def test_requires_internal_secret(self, client):
        resp = client.get("/v1/generator-profiles")
        assert resp.status_code == 401

    def test_list_forwards_filters(self, client):
        expected = [_profile_out(_id(1))]
        with patch(
            "app.stores.generator_profiles.list_profiles",
            new=AsyncMock(return_value=expected),
        ) as m:
            resp = client.get(
                "/v1/generator-profiles",
                headers=_headers(),
                params={"status": "draft", "slug": "strawberina-sdxl"},
            )
        assert resp.status_code == 200
        assert resp.json()[0]["slug"] == "strawberina-sdxl"
        assert m.call_args.kwargs == {
            "status": "draft",
            "user_id": None,
            "slug": "strawberina-sdxl",
        }

    def test_create_v1(self, client):
        expected = _profile_out(_id(1), version=1)
        with patch(
            "app.stores.generator_profiles.create_profile",
            new=AsyncMock(return_value=expected),
        ) as m:
            resp = client.post(
                "/v1/generator-profiles",
                headers=_headers(),
                json={"slug": "strawberina-sdxl", "name": "Strawberina SDXL", "spec": _spec()},
            )
        assert resp.status_code == 200
        assert resp.json()["version"] == 1
        assert resp.json()["status"] == "draft"
        assert isinstance(m.call_args.args[1], GeneratorProfileCreate)

    def test_create_draft_conflict_returns_409(self, client):
        with patch(
            "app.stores.generator_profiles.create_profile",
            new=AsyncMock(side_effect=GeneratorProfileError(409, "a draft already exists for slug 'x'")),
        ):
            resp = client.post(
                "/v1/generator-profiles",
                headers=_headers(),
                json={"slug": "x", "name": "X", "spec": _spec()},
            )
        assert resp.status_code == 409
        assert "draft already exists" in resp.json()["detail"]

    def test_get_by_id_404(self, client):
        with patch(
            "app.stores.generator_profiles.get_profile",
            new=AsyncMock(return_value=None),
        ):
            resp = client.get(f"/v1/generator-profiles/{_id(9)}", headers=_headers())
        assert resp.status_code == 404

    def test_by_ref_lookup(self, client):
        expected = _profile_out(_id(1), version=1)
        with patch(
            "app.stores.generator_profiles.get_profile_by_ref",
            new=AsyncMock(return_value=expected),
        ) as m:
            resp = client.get(
                "/v1/generator-profiles/by-ref/strawberina-sdxl@1", headers=_headers()
            )
        assert resp.status_code == 200
        assert resp.json()["version"] == 1
        assert m.call_args.args[1] == "strawberina-sdxl@1"

    def test_by_ref_404_when_absent(self, client):
        with patch(
            "app.stores.generator_profiles.get_profile_by_ref",
            new=AsyncMock(return_value=None),
        ):
            resp = client.get(
                "/v1/generator-profiles/by-ref/strawberina-sdxl@9", headers=_headers()
            )
        assert resp.status_code == 404

    def test_by_ref_malformed_returns_400(self, client):
        with patch(
            "app.stores.generator_profiles.get_profile_by_ref",
            new=AsyncMock(side_effect=GeneratorProfileError(400, "ref must be of the form 'slug@version'")),
        ):
            resp = client.get(
                "/v1/generator-profiles/by-ref/not-a-ref", headers=_headers()
            )
        assert resp.status_code == 400

    def test_update_published_returns_409(self, client):
        with patch(
            "app.stores.generator_profiles.update_profile",
            new=AsyncMock(side_effect=GeneratorProfileError(409, "published profiles are immutable")),
        ):
            resp = client.put(
                f"/v1/generator-profiles/{_id(1)}",
                headers=_headers(),
                json={"name": "new"},
            )
        assert resp.status_code == 409

    def test_update_404(self, client):
        with patch(
            "app.stores.generator_profiles.update_profile",
            new=AsyncMock(return_value=None),
        ):
            resp = client.put(
                f"/v1/generator-profiles/{_id(1)}", headers=_headers(), json={"name": "n"}
            )
        assert resp.status_code == 404

    def test_publish(self, client):
        expected = _profile_out(_id(1), status="published")
        with patch(
            "app.stores.generator_profiles.publish_profile",
            new=AsyncMock(return_value=expected),
        ):
            resp = client.post(
                f"/v1/generator-profiles/{_id(1)}/publish", headers=_headers()
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "published"

    def test_publish_already_published_409(self, client):
        with patch(
            "app.stores.generator_profiles.publish_profile",
            new=AsyncMock(side_effect=GeneratorProfileError(409, "profile is already published")),
        ):
            resp = client.post(
                f"/v1/generator-profiles/{_id(1)}/publish", headers=_headers()
            )
        assert resp.status_code == 409

    def test_delete_draft(self, client):
        with patch(
            "app.stores.generator_profiles.delete_profile",
            new=AsyncMock(return_value=True),
        ):
            resp = client.delete(
                f"/v1/generator-profiles/{_id(1)}", headers=_headers()
            )
        assert resp.status_code == 204

    def test_delete_published_409(self, client):
        with patch(
            "app.stores.generator_profiles.delete_profile",
            new=AsyncMock(side_effect=GeneratorProfileError(409, "published profiles cannot be deleted")),
        ):
            resp = client.delete(
                f"/v1/generator-profiles/{_id(1)}", headers=_headers()
            )
        assert resp.status_code == 409

    def test_delete_absent_404(self, client):
        with patch(
            "app.stores.generator_profiles.delete_profile",
            new=AsyncMock(return_value=None),
        ):
            resp = client.delete(
                f"/v1/generator-profiles/{_id(1)}", headers=_headers()
            )
        assert resp.status_code == 404


# ── store-level tests (mocked session emulating queries) ──────────────────


class _FakeProfileRow:
    def __init__(self, *, id_, slug, version, status, name="N", media_type="image", spec=None, user_id=None):
        self.id = id_
        self.slug = slug
        self.version = version
        self.status = status
        self.user_id = user_id
        self.name = name
        self.media_type = media_type
        self.spec = spec or {}
        now = datetime.now(timezone.utc)
        self.created_at = now
        self.updated_at = now


def _scalar_result(value):
    """Emulate session.execute(...).scalar_one()/scalar_one_or_none()."""
    res = MagicMock()
    res.scalar_one = MagicMock(return_value=value)
    res.scalar_one_or_none = MagicMock(return_value=value)
    return res


def _first_result(value):
    """Emulate session.execute(...).first()."""
    res = MagicMock()
    res.first = MagicMock(return_value=value)
    return res


async def _refresh_server_defaults(row):
    """Emulate refresh() repopulating server-default id/timestamp columns."""
    if getattr(row, "id", None) is None:
        row.id = _id(999)
    now = datetime.now(timezone.utc)
    row.created_at = now
    row.updated_at = now


class TestGeneratorProfileStore:
    @pytest.mark.asyncio
    async def test_create_first_version_is_v1_draft(self):
        session = MagicMock()
        session.commit = AsyncMock()
        session.refresh = AsyncMock(side_effect=_refresh_server_defaults)
        added = []
        session.add = MagicMock(side_effect=lambda r: added.append(r))
        # 1st execute: existing-draft probe -> none; 2nd: max(version) -> None
        session.execute = AsyncMock(side_effect=[_first_result(None), _scalar_result(None)])

        body = GeneratorProfileCreate(slug="newslug", name="New", spec=_spec())
        out = await generator_profiles.create_profile(session, body)
        assert added[0].version == 1
        assert added[0].status == "draft"
        assert out.version == 1

    @pytest.mark.asyncio
    async def test_create_bumps_version(self):
        session = MagicMock()
        session.commit = AsyncMock()
        session.refresh = AsyncMock(side_effect=_refresh_server_defaults)
        added = []
        session.add = MagicMock(side_effect=lambda r: added.append(r))
        # no existing draft, max(version) == 3 -> next is 4
        session.execute = AsyncMock(side_effect=[_first_result(None), _scalar_result(3)])

        body = GeneratorProfileCreate(slug="strawberina-sdxl", name="V4", spec=_spec())
        await generator_profiles.create_profile(session, body)
        assert added[0].version == 4

    @pytest.mark.asyncio
    async def test_create_rejects_second_draft(self):
        session = MagicMock()
        session.commit = AsyncMock()
        # existing-draft probe returns a row -> 409 before max(version) is queried
        session.execute = AsyncMock(side_effect=[_first_result(("some-id",))])

        body = GeneratorProfileCreate(slug="strawberina-sdxl", name="dup", spec=_spec())
        with pytest.raises(GeneratorProfileError) as exc:
            await generator_profiles.create_profile(session, body)
        assert exc.value.status_code == 409
        session.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_create_validates_spec_required_keys(self):
        session = MagicMock()
        body = GeneratorProfileCreate(slug="s", name="n", spec={"base_model": {}})
        with pytest.raises(GeneratorProfileError) as exc:
            await generator_profiles.create_profile(session, body)
        assert exc.value.status_code == 400

    @pytest.mark.asyncio
    async def test_create_validates_adapters_is_list(self):
        session = MagicMock()
        bad = _spec()
        bad["adapters"] = {"not": "a list"}
        body = GeneratorProfileCreate(slug="s", name="n", spec=bad)
        with pytest.raises(GeneratorProfileError) as exc:
            await generator_profiles.create_profile(session, body)
        assert exc.value.status_code == 400
        assert "adapters" in exc.value.message

    @pytest.mark.asyncio
    async def test_update_draft_changes_name_and_spec(self):
        row = _FakeProfileRow(id_=_id(1), slug="s", version=1, status="draft", name="old")
        session = MagicMock()
        session.get = AsyncMock(return_value=row)
        session.commit = AsyncMock()
        session.refresh = AsyncMock()

        out = await generator_profiles.update_profile(
            session, _id(1), GeneratorProfileUpdate(name="new")
        )
        assert row.name == "new"
        assert out.name == "new"

    @pytest.mark.asyncio
    async def test_update_published_raises_409(self):
        row = _FakeProfileRow(id_=_id(1), slug="s", version=1, status="published")
        session = MagicMock()
        session.get = AsyncMock(return_value=row)
        session.commit = AsyncMock()

        with pytest.raises(GeneratorProfileError) as exc:
            await generator_profiles.update_profile(
                session, _id(1), GeneratorProfileUpdate(name="x")
            )
        assert exc.value.status_code == 409
        session.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_publish_freezes_draft(self):
        row = _FakeProfileRow(id_=_id(1), slug="s", version=1, status="draft")
        session = MagicMock()
        session.get = AsyncMock(return_value=row)
        session.commit = AsyncMock()
        session.refresh = AsyncMock()

        out = await generator_profiles.publish_profile(session, _id(1))
        assert row.status == "published"
        assert out.status == "published"

    @pytest.mark.asyncio
    async def test_publish_already_published_raises_409(self):
        row = _FakeProfileRow(id_=_id(1), slug="s", version=1, status="published")
        session = MagicMock()
        session.get = AsyncMock(return_value=row)
        session.commit = AsyncMock()
        with pytest.raises(GeneratorProfileError) as exc:
            await generator_profiles.publish_profile(session, _id(1))
        assert exc.value.status_code == 409

    @pytest.mark.asyncio
    async def test_delete_published_raises_409(self):
        row = _FakeProfileRow(id_=_id(1), slug="s", version=1, status="published")
        session = MagicMock()
        session.get = AsyncMock(return_value=row)
        session.commit = AsyncMock()
        session.execute = AsyncMock()
        with pytest.raises(GeneratorProfileError) as exc:
            await generator_profiles.delete_profile(session, _id(1))
        assert exc.value.status_code == 409
        session.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_delete_draft_succeeds(self):
        row = _FakeProfileRow(id_=_id(1), slug="s", version=1, status="draft")
        session = MagicMock()
        session.get = AsyncMock(return_value=row)
        session.commit = AsyncMock()
        session.execute = AsyncMock()
        ok = await generator_profiles.delete_profile(session, _id(1))
        assert ok is True
        session.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_delete_absent_returns_none(self):
        session = MagicMock()
        session.get = AsyncMock(return_value=None)
        out = await generator_profiles.delete_profile(session, _id(1))
        assert out is None

    @pytest.mark.asyncio
    async def test_by_ref_parses_and_queries(self):
        row = _FakeProfileRow(id_=_id(1), slug="strawberina-sdxl", version=2, status="published")
        session = MagicMock()
        session.execute = AsyncMock(return_value=_scalar_result(row))
        out = await generator_profiles.get_profile_by_ref(session, "strawberina-sdxl@2")
        assert out.version == 2
        assert out.slug == "strawberina-sdxl"

    @pytest.mark.asyncio
    async def test_by_ref_malformed_raises_400(self):
        session = MagicMock()
        for bad in ["not-a-ref", "slug@", "@1", "slug@notint"]:
            with pytest.raises(GeneratorProfileError) as exc:
                await generator_profiles.get_profile_by_ref(session, bad)
            assert exc.value.status_code == 400

    def test_parse_ref_with_at_in_slug(self):
        # rpartition keeps everything before the last '@' as the slug.
        slug, version = generator_profiles.parse_ref("my@weird@slug@5")
        assert slug == "my@weird@slug"
        assert version == 5


# ── character generator_profile_id round-trip ─────────────────────────────


class _FakeCharacterRow:
    def __init__(self):
        self.id = _id(50)
        self.series_id = _id(60)
        self.name = "Hero"
        self.description = ""
        self.voice = ""
        self.reference_image_media_id = None
        self.generator_profile_id = None
        self.metadata_ = {}
        now = datetime.now(timezone.utc)
        self.created_at = now
        self.updated_at = now


class TestCharacterGeneratorProfileLink:
    def test_character_out_includes_generator_profile_id(self):
        row = _FakeCharacterRow()
        row.generator_profile_id = _id(1)
        out = CharacterOut.from_orm_row(row)
        assert out.generator_profile_id == _id(1)

    def test_upsert_character_persists_generator_profile_id(self, client):
        from app.schemas import CharacterIn
        from app.stores import characters

        captured = {}

        async def _fake_upsert(session, body: CharacterIn):
            captured["gpid"] = body.generator_profile_id
            row = _FakeCharacterRow()
            row.id = body.id
            row.series_id = body.series_id
            row.name = body.name
            row.generator_profile_id = body.generator_profile_id
            return CharacterOut.from_orm_row(row)

        with patch.object(characters, "upsert_character", new=_fake_upsert):
            resp = client.put(
                f"/v1/characters/{_id(50)}",
                headers=_headers(),
                json={
                    "id": _id(50),
                    "series_id": _id(60),
                    "name": "Hero",
                    "generator_profile_id": _id(1),
                },
            )
        assert resp.status_code == 200
        assert resp.json()["generator_profile_id"] == _id(1)
        assert captured["gpid"] == _id(1)
