from __future__ import annotations

import json
import logging
import os
import sys
import time
import traceback
import uuid
from contextvars import ContextVar
from typing import Any

from sqlalchemy.exc import StatementError

_request_id: ContextVar[str] = ContextVar("request_id", default="")
_user_id: ContextVar[str] = ContextVar("user_id", default="")


def set_request_id(value: str) -> None:
    _request_id.set(value or "")


def set_user_id(value: str) -> None:
    _user_id.set(value or "")


def current_request_id() -> str:
    return _request_id.get()


class JSONFormatter(logging.Formatter):
    _RESERVED = {
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
        "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
        "created", "msecs", "relativeCreated", "thread", "threadName",
        "processName", "process", "message", "taskName", "asctime",
    }

    def __init__(self, service: str):
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)),
            "level": record.levelname.lower(),
            "service": self.service,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        rid = _request_id.get()
        if rid:
            payload["request_id"] = rid
        uid = _user_id.get()
        if uid:
            payload["user_id"] = uid
        for key, value in record.__dict__.items():
            if key in self._RESERVED or key.startswith("_"):
                continue
            try:
                json.dumps(value)
                payload[key] = value
            except (TypeError, ValueError):
                payload[key] = repr(value)
        if record.exc_info:
            # uvicorn logs every unhandled error a second time (uvicorn.error, "Exception in ASGI
            # application", exc_info=the exception): no caller of ours to ask for care, so this is
            # where a database error is cut down to its summary and frames.
            exc = record.exc_info[1]
            err = db_error_in(exc)
            payload["exc"] = self.formatException(record.exc_info) if err is None else db_error_traceback(err, exc)
        return json.dumps(payload, ensure_ascii=False)


def init_logging(service: str) -> None:
    level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter(service=service))
    root.addHandler(handler)
    root.setLevel(level)
    for noisy in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(noisy)
        lg.handlers = [handler]
        lg.propagate = False
        lg.setLevel(level)


def new_request_id() -> str:
    return uuid.uuid4().hex


# A database error's text is not safe to log. SQLAlchemy appends the statement and, unless the engine
# hides them (app.db.make_engine does), its bound values; the driver's message carries more of its
# own: Postgres adds `DETAIL: Failing row contains (...)` / `Key (col)=(value) ...` lines under the
# first, which name the values of the row (prompts, file_url, usernames, password hashes). Only the
# first line (it names the constraint or the failure) is logged, with the frames.


def db_error_in(exc: BaseException | None) -> StatementError | None:
    """The SQLAlchemy StatementError (DBAPIError and its subclasses) that ``exc`` is or was raised
    from, looking through __cause__/__context__; None when no database error is involved."""
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        if isinstance(exc, StatementError):
            return exc
        seen.add(id(exc))
        exc = exc.__cause__ or exc.__context__
    return None


def first_line(error: object, limit: int = 300) -> str:
    """The first line of an error's message: for a database error, the line that names the constraint."""
    lines = str(error).splitlines() if error is not None else []
    return lines[0][:limit] if lines else ""


def db_error_summary(err: StatementError) -> str:
    """`<driver error class>: <first line of the database's message>`, nothing else."""
    return f"{type(err.orig).__name__}: {first_line(err.orig)}"


def db_error_traceback(err: StatementError, exc: BaseException) -> str:
    """The stack frames of ``exc`` (and of ``err`` when it is the cause behind it) and the error's
    summary: the shape of a traceback, without the exception text."""
    parts = ["Traceback (most recent call last):\n"]
    for e in (exc,) if err is exc else (exc, err):
        parts.append("".join(traceback.format_tb(e.__traceback__)))
    parts.append(f"{type(err).__module__}.{type(err).__name__}: {db_error_summary(err)}")
    return "".join(parts)


def log_db_error(
    log: logging.Logger, level: int, msg: str, err: StatementError, exc: BaseException, *args: object
) -> None:
    """Logs ``msg % args`` plus the database error's summary, and db_error_traceback in the JSON line's
    "exc" field. Never passes exc_info, so no formatter can print the message body."""
    log.log(
        level, "%s: %s", msg % args if args else msg, db_error_summary(err),
        extra={"exc": db_error_traceback(err, exc)},
    )


def log_exception(log: logging.Logger, level: int, msg: str, *args: object, exc: BaseException) -> None:
    """``log.log(level, msg, *args, exc_info=exc)``, except that a database error is logged by
    log_db_error. ``args`` must not carry the exception itself."""
    err = db_error_in(exc)
    if err is None:
        log.log(level, msg, *args, exc_info=exc)
    else:
        log_db_error(log, level, msg, err, exc, *args)


def exception_text(exc: BaseException) -> str:
    """`<class>: <message>` for a status line a client can read (/healthz): the first line only for a
    database error, the first 300 characters of the message otherwise."""
    err = db_error_in(exc)
    if err is None:
        return f"{type(exc).__name__}: {str(exc)[:300]}"
    return f"{type(exc).__name__}: {db_error_summary(err)}"
