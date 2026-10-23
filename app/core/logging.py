"""Structured JSON logging for the B-roll engine."""
from __future__ import annotations

import json
import logging
import sys
import time
from typing import Any, Dict, Optional
from contextvars import ContextVar

from app.core.config import settings

# Context variable to hold the current request's correlation ID
_correlation_id: ContextVar[str] = ContextVar("correlation_id", default="-")
_job_id: ContextVar[str] = ContextVar("job_id", default="-")


def set_correlation_id(cid: str) -> None:
    _correlation_id.set(cid)


def set_job_id(jid: str) -> None:
    _job_id.set(jid)


class StructuredFormatter(logging.Formatter):
    """Emit log records as single-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:  # noqa: A003
        payload: Dict[str, Any] = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "job_id": _job_id.get(),
            "x_correlation_id": _correlation_id.get(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        # Merge any extra fields passed to the log call
        for key, value in record.__dict__.items():
            if key not in (
                "args", "asctime", "created", "exc_info", "exc_text", "filename",
                "funcName", "id", "levelname", "levelno", "lineno", "module",
                "msecs", "message", "msg", "name", "pathname", "process",
                "processName", "relativeCreated", "stack_info", "thread", "threadName",
            ) and not key.startswith("_"):
                payload[key] = value
        return json.dumps(payload, default=str)


def configure_logging() -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO))
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(StructuredFormatter())
    root.handlers.clear()
    root.addHandler(handler)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
