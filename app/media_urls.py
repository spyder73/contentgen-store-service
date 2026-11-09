"""Which file_url a media row may carry.

A media row's file_url is read back by the Go backend, which turns it into local
file operations (read, rename, remove) and into reads of remote URLs. It comes from
the row's owner, so it is validated where it is written: empty, one flat file of the
upload pool (/media/uploads/<name>) or of the generators' assets dir
(/assets/<name>, which also holds the _waiting.png and _failed.png placeholders), or
an http(s) URL with a host. <name> is a plain flat name: it does not start with a dot, has no
"/", "..", backslash or "%" (a percent-encoded "/" or ".." is decoded further along), no control
character and no whitespace at either end.
"""
from __future__ import annotations

from urllib.parse import urlsplit

_PREFIXES = ("/media/uploads/", "/assets/")


def _flat_name(name: str) -> bool:
    return (
        bool(name)
        and name == name.strip()
        and not name.startswith(".")
        and "/" not in name
        and ".." not in name
        and "\\" not in name
        and "%" not in name
        and all(ord(c) >= 32 and ord(c) != 127 for c in name)
    )


def valid_file_url(value: str | None) -> bool:
    """True for a file_url that may be stored; None (not given) and "" (none) are fine."""
    if not value:
        return True
    if value.startswith(("http://", "https://")):
        try:
            parts = urlsplit(value)
        except ValueError:
            return False
        return bool(parts.netloc) and value == value.strip() and all(ord(c) >= 32 and ord(c) != 127 for c in value)
    return any(value.startswith(p) and _flat_name(value[len(p):]) for p in _PREFIXES)
