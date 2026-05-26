from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.db import get_session
from app.fastapi_app import create_fastapi_app
from app.schemas import RenderProposalOut, RenderTemplateOut

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


def _headers(user_id: str) -> dict[str, str]:
    return {"X-User-ID": user_id, "X-Internal-Secret": INTERNAL_SECRET}


def _id(n: int) -> str:
    return f"00000000-0000-0000-0000-{n:012d}"


def _template_out(id_: str, user_id: str | None, **overrides) -> RenderTemplateOut:
    now = datetime.now(timezone.utc)
    data = {
        "id": id_,
        "user_id": user_id,
        "name": "Clean Carousel",
        "description": "",
        "kind": "carousel",
        "source": "user_saved",
        "status": "active",
        "config": {"layout": "split"},
        "preview_url": None,
        "created_from_clip_id": None,
        "created_from_instruction": None,
        "created_at": now,
        "updated_at": now,
    }
    data.update(overrides)
    return RenderTemplateOut(**data)


def _proposal_out(id_: str, user_id: str, **overrides) -> RenderProposalOut:
    now = datetime.now(timezone.utc)
    data = {
        "id": id_,
        "clip_id": _id(900),
        "user_id": user_id,
        "kind": "carousel_design",
        "status": "draft",
        "instruction": "Make it editorial",
        "source_template_id": None,
        "metadata_patch_json": {"headline": "New"},
        "template_config_json": {"layout": "stacked"},
        "preview_output_refs_json": [],
        "validation_report_json": {},
        "created_at": now,
        "approved_at": None,
    }
    data.update(overrides)
    return RenderProposalOut(**data)


class TestRenderTemplates:
    def test_create_template_defaults_to_header_user(self, client):
        uid = _id(1)
        tid = _id(101)
        expected = _template_out(tid, uid)
        with patch(
            "app.stores.render_templates.create_template",
            new=AsyncMock(return_value=expected),
        ) as m:
            resp = client.post(
                "/v1/render-templates",
                headers=_headers(uid),
                json={"name": "Clean Carousel", "config": {"layout": "split"}},
            )
        assert resp.status_code == 200
        assert resp.json()["user_id"] == uid
        assert m.call_args.kwargs["user_id"] == uid

    def test_create_template_requires_user_id(self, client):
        resp = client.post(
            "/v1/render-templates",
            headers={"X-Internal-Secret": INTERNAL_SECRET},
            json={"name": "No User"},
        )
        assert resp.status_code == 401

    def test_list_templates_forwards_filters_and_user(self, client):
        uid = _id(1)
        expected = [_template_out(_id(102), uid)]
        with patch(
            "app.stores.render_templates.list_templates",
            new=AsyncMock(return_value=expected),
        ) as m:
            resp = client.get(
                "/v1/render-templates",
                headers=_headers(uid),
                params={"kind": "carousel", "include_archived": "true"},
            )
        assert resp.status_code == 200
        assert len(resp.json()) == 1
        assert m.call_args.kwargs == {
            "user_id": uid,
            "kind": "carousel",
            "include_archived": True,
        }

    def test_get_template_returns_404_for_inaccessible_template(self, client):
        uid = _id(1)
        tid = _id(103)
        with patch(
            "app.stores.render_templates.get_template",
            new=AsyncMock(return_value=None),
        ) as m:
            resp = client.get(f"/v1/render-templates/{tid}", headers=_headers(uid))
        assert resp.status_code == 404
        assert m.call_args.kwargs["user_id"] == uid

    def test_get_template_allows_global_template(self, client):
        uid = _id(1)
        tid = "builtin-carousel"
        expected = _template_out(tid, None, source="builtin")
        with patch(
            "app.stores.render_templates.get_template",
            new=AsyncMock(return_value=expected),
        ):
            resp = client.get(f"/v1/render-templates/{tid}", headers=_headers(uid))
        assert resp.status_code == 200
        assert resp.json()["user_id"] is None

    def test_update_template(self, client):
        uid = _id(1)
        tid = _id(104)
        expected = _template_out(tid, uid, name="Updated Carousel", status="draft")
        with patch(
            "app.stores.render_templates.update_template",
            new=AsyncMock(return_value=expected),
        ) as m:
            resp = client.put(
                f"/v1/render-templates/{tid}",
                headers=_headers(uid),
                json={"name": "Updated Carousel", "status": "draft"},
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "draft"
        assert m.call_args.args[1] == tid
        assert m.call_args.kwargs["user_id"] == uid

    def test_delete_template_archives(self, client):
        uid = _id(1)
        tid = _id(105)
        with patch(
            "app.stores.render_templates.archive_template",
            new=AsyncMock(return_value=True),
        ) as m:
            resp = client.delete(f"/v1/render-templates/{tid}", headers=_headers(uid))
        assert resp.status_code == 204
        assert m.call_args.kwargs["user_id"] == uid

    def test_clone_template(self, client):
        uid = _id(1)
        source_id = "builtin-carousel"
        clone_id = _id(106)
        expected = _template_out(clone_id, uid, name="My Clone")
        with patch(
            "app.stores.render_templates.clone_template",
            new=AsyncMock(return_value=expected),
        ) as m:
            resp = client.post(
                f"/v1/render-templates/{source_id}/clone",
                headers=_headers(uid),
                json={"id": clone_id, "name": "My Clone"},
            )
        assert resp.status_code == 200
        assert resp.json()["id"] == clone_id
        assert m.call_args.kwargs["user_id"] == uid


class TestRenderProposals:
    def test_create_proposal(self, client):
        uid = _id(1)
        pid = _id(201)
        expected = _proposal_out(pid, uid)
        with patch(
            "app.stores.render_templates.create_proposal",
            new=AsyncMock(return_value=expected),
        ) as m:
            resp = client.post(
                "/v1/render-proposals",
                headers=_headers(uid),
                json={"instruction": "Make it editorial"},
            )
        assert resp.status_code == 200
        assert resp.json()["user_id"] == uid
        assert m.call_args.kwargs["user_id"] == uid

    def test_list_proposals_for_clip(self, client):
        uid = _id(1)
        clip_id = _id(301)
        expected = [_proposal_out(_id(202), uid, clip_id=clip_id)]
        with patch(
            "app.stores.render_templates.list_proposals",
            new=AsyncMock(return_value=expected),
        ) as m:
            resp = client.get(
                "/v1/render-proposals",
                headers=_headers(uid),
                params={"clip_id": clip_id},
            )
        assert resp.status_code == 200
        assert resp.json()[0]["clip_id"] == clip_id
        assert m.call_args.kwargs == {"user_id": uid, "clip_id": clip_id}

    def test_get_proposal_returns_404_for_other_user(self, client):
        uid = _id(1)
        pid = _id(203)
        with patch(
            "app.stores.render_templates.get_proposal",
            new=AsyncMock(return_value=None),
        ) as m:
            resp = client.get(f"/v1/render-proposals/{pid}", headers=_headers(uid))
        assert resp.status_code == 404
        assert m.call_args.kwargs["user_id"] == uid

    def test_patch_proposal_status(self, client):
        uid = _id(1)
        pid = _id(204)
        expected = _proposal_out(pid, uid, status="approved", approved_at=datetime.now(timezone.utc))
        with patch(
            "app.stores.render_templates.set_proposal_status",
            new=AsyncMock(return_value=expected),
        ) as m:
            resp = client.patch(
                f"/v1/render-proposals/{pid}/status",
                headers=_headers(uid),
                json={
                    "status": "approved",
                    "validation_report_json": {"ok": True},
                },
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "approved"
        assert m.call_args.args[1] == pid
        assert m.call_args.args[2] == "approved"
        assert m.call_args.kwargs["validation_report_json"] == {"ok": True}
