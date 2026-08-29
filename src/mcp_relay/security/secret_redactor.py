"""Redact API keys and other secrets from log output and error messages."""

from __future__ import annotations

import re

# Matches "api key <value>" with alphanumeric + common key chars
_API_KEY_RE = re.compile(r"(?i)(api[\s_-]*key[\s:=]+)[A-Za-z0-9\-_.+/]{8,}")


def redact(text: str) -> str:
    return _API_KEY_RE.sub(r"\1[REDACTED]", text)
