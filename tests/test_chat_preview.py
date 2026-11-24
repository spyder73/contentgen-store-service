"""preview_line and last_line: the one plain line of a chat list row."""
from __future__ import annotations

import pytest

from app.stores.chat_preview import last_line, preview_line

UUID = "0b5c1c9e-6d47-4c3a-9a55-2f4f3c1d9e11"


@pytest.mark.parametrize(
    "text, expected",
    [
        ('Here is **the** plan:\n\n```json\n{"a":1}\n```\nDone.', "Here is the plan: Done."),
        ("# Title\n- one\n- two", "Title one two"),
        (f"See [the clip](clip:{UUID}) and media:{UUID}", "See the clip and"),
        ("```\n```", ""),
        ("before ```never closed\nsecret", "before"),
        ("![a cat](media:x) > quoted ~~gone~~ `code`", "a cat > quoted gone code"),
        ("> quote\n1. first\n2) second", "quote first second"),
    ],
)
def test_preview_line_flattens_markdown(text, expected):
    assert preview_line(text) == expected


def test_a_long_line_is_cut_at_a_word():
    line = preview_line("word " * 500)
    assert len(line) <= 140 and line.endswith("…")
    assert set(line[:-1].split()) == {"word"}


def test_a_long_word_is_cut_hard():
    line = preview_line("a" * 10000)
    assert line == "a" * 139 + "…"


def test_only_the_first_4000_characters_are_read():
    assert preview_line("x " * 3000 + "tail") .endswith("…")
    assert "tail" not in preview_line(" " * 3999 + "tail")


@pytest.mark.parametrize("kind", ["tool", "run", "node"])
def test_steps_never_become_a_last_line(kind):
    assert last_line("operator", kind, "x") is None


def test_media_without_text_and_empty_previews_have_no_line():
    assert last_line("operator", "media", "") is None
    assert last_line("operator", "text", "```\n```") is None


def test_a_user_text_keeps_its_role_and_kind():
    assert last_line("user", "text", "  hi **there** ") == ("user", "text", "hi there")
    assert last_line("operator", "event", "Routine run: ok") == ("operator", "event", "Routine run: ok")
