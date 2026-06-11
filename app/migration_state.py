from __future__ import annotations

"""Process-wide record of the startup migration outcome.

Lives in its own module so both app.main (which runs migrations) and
app.fastapi_app (which reports the outcome on /healthz) can use it without a
circular import.
"""

_status: str = "not run"


def set_migration_status(status: str) -> None:
    global _status
    _status = status


def get_migration_status() -> str:
    return _status
