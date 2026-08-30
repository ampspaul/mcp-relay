"""Structured JSON logging configuration."""

from __future__ import annotations

import json
import logging
import os
import time


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def configure() -> None:
    """Apply JSON or text formatting to the root logger based on LOG_FORMAT env var.

    LOG_FORMAT=json  → structured JSON (default)
    LOG_FORMAT=text  → human-readable (rich/uvicorn style)
    """
    fmt = os.environ.get("LOG_FORMAT", "json").strip().lower()
    if fmt == "json":
        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter())
        root = logging.getLogger()
        root.handlers.clear()
        root.addHandler(handler)
