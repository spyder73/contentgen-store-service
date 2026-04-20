from __future__ import annotations

import json
import logging
import os
import sys
import time
import uuid
from contextvars import ContextVar
from typing import Any

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
            payload["exc"] = self.formatException(record.exc_info)
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
