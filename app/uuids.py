"""The one spelling a uuid is compared and stored in.

uuid.UUID also takes uppercase, braces, a urn: prefix and no hyphens, and Postgres
reads every one of them as the same uuid, so an id (a user's, a media row's) is
never compared or stored as the caller happened to type it.
"""
from __future__ import annotations

import uuid


def uuid_form(value) -> str | None:
    """``value`` as a canonical uuid string (lowercase, hyphenated), else None."""
    try:
        return str(uuid.UUID(str(value).strip()))
    except ValueError:
        return None


def canonical(value):
    """``value`` as a canonical uuid string; anything that is not a uuid unchanged."""
    return uuid_form(value) or value
