"""Tests for the seed-if-missing built-in prompt-template route and store fn.

The Go backend re-seeds its local-asset prompt templates on every startup, so
the seed route must:
  - create a row when none exists (so refs from other services resolve), and
  - NEVER overwrite an existing row (so user edits to built-ins survive restarts).
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.db import get_session
from app.fastapi_app import create_fastapi_app
from app.schemas import PromptTemplateIn, PromptTemplateOut
from app.stores import prompts

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


def _prompt_out(id_: str, name: str, content: str, visibility: str = "global") -> PromptTemplateOut:
    now = datetime.now(timezone.utc)
    return PromptTemplateOut(
        id=id_,
        name=name,
        content=content,
        metadata={},
        user_id=None,
        visibility=visibility,
        created_at=now,
        updated_at=now,
    )


class _FakeRow:
    """Stand-in ORM row for the prompt_templates table."""

    def __init__(self, id_: str, name: str, content: str, visibility: str = "global"):
        self.id = id_
        self.name = name
        self.content = content
        self.metadata_ = {}
        self.user_id = None
        self.visibility = visibility
        now = datetime.now(timezone.utc)
        self.created_at = now
        self.updated_at = now


class TestSeedRoute:
    def test_seed_requires_internal_secret(self, client):
        resp = client.put(
            "/v1/admin/prompt-templates/generator-pass-through-prompt",
            json={"id": "x", "name": "n", "content": "c"},
        )
        assert resp.status_code == 401

    def test_seed_forwards_body_and_defaults_global(self, client):
        expected = _prompt_out("generator-pass-through-prompt", "Pass Through", "{{prompt}}")
        with patch(
            "app.stores.prompts.seed_prompt",
            new=AsyncMock(return_value=expected),
        ) as m:
            resp = client.put(
                "/v1/admin/prompt-templates/generator-pass-through-prompt",
                headers={"X-Internal-Secret": INTERNAL_SECRET},
                json={"id": "ignored", "name": "Pass Through", "content": "{{prompt}}"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == "generator-pass-through-prompt"
        assert body["visibility"] == "global"
        # path id wins over body id, and the route forced global visibility
        sent = m.call_args.args[1]
        assert sent.id == "generator-pass-through-prompt"
        assert sent.visibility == "global"


class TestSeedStore:
    @pytest.mark.asyncio
    async def test_seed_creates_when_missing(self):
        session = MagicMock()
        session.get = AsyncMock(return_value=None)
        session.commit = AsyncMock()
        session.refresh = AsyncMock()
        added = []
        session.add = MagicMock(side_effect=lambda row: added.append(row))

        body = PromptTemplateIn(
            id="builtin-x", name="Built In", content="hello", visibility="global"
        )
        # refresh() would normally repopulate server-default timestamps; emulate it.
        async def _refresh(row):
            now = datetime.now(timezone.utc)
            row.created_at = now
            row.updated_at = now

        session.refresh = AsyncMock(side_effect=_refresh)

        out = await prompts.seed_prompt(session, body)
        assert len(added) == 1
        assert added[0].id == "builtin-x"
        assert added[0].visibility == "global"
        assert out.id == "builtin-x"
        session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_seed_does_not_overwrite_existing(self):
        existing = _FakeRow("builtin-x", "User Edited Name", "user edited content")
        session = MagicMock()
        session.get = AsyncMock(return_value=existing)
        session.commit = AsyncMock()
        session.add = MagicMock()

        body = PromptTemplateIn(
            id="builtin-x", name="Shipped Name", content="shipped content", visibility="global"
        )
        out = await prompts.seed_prompt(session, body)

        # The existing row is returned untouched and nothing is committed.
        assert out.name == "User Edited Name"
        assert out.content == "user edited content"
        session.add.assert_not_called()
        session.commit.assert_not_awaited()
