"""The one plain line a chat list row shows for a message.

``preview_line`` flattens a message's markdown to a single line of at most ``limit`` characters.
``last_line`` says which messages may become a thread's last line at all (a tool step, a run or a
node is not something the owner said or was told).
"""

from __future__ import annotations

import re

READ_LIMIT = 4000
LINE_LIMIT = 140
NO_LINE_KINDS = frozenset({"tool", "run", "node"})

_FENCE = re.compile(r"```.*?(?:```|\Z)", re.DOTALL)
_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_REF = re.compile(r"\b(?:media|clip):[\w-]+")
_LINE_MARK = re.compile(r"^[ \t]{0,3}(?:(?:#{1,6}|>|[-*+]|\d+[.)])[ \t]+|>)+", re.MULTILINE)
_EMPHASIS = re.compile(r"\*\*|__|~~|`")


def preview_line(text: str, limit: int = LINE_LIMIT) -> str:
    """``text`` as one plain line: no code blocks, refs or markdown marks, cut at a word."""
    flat = _FENCE.sub(" ", text[:READ_LIMIT])
    flat = _LINK.sub(r"\1", _IMAGE.sub(r"\1", flat))
    flat = _REF.sub(" ", flat)
    flat = _EMPHASIS.sub("", _LINE_MARK.sub("", flat))
    flat = " ".join(flat.split())
    if len(flat) <= limit:
        return flat
    head = flat[: limit - 1]
    if flat[limit - 1] != " " and (space := head.rfind(" ")) > 0:
        head = head[:space]
    return head.rstrip() + "…"


def last_line(role: str, kind: str, text: str) -> tuple[str, str, str] | None:
    """``(role, kind, preview)`` to store as a thread's last line; None when the message has none."""
    if kind in NO_LINE_KINDS:
        return None
    preview = preview_line(text)
    return (role, kind, preview) if preview else None
