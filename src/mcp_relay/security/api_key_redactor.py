"""Redact API key patterns from log output and tool results.

Covers patterns of the form  'api key <value>'  /  'api-key: <value>'  /
'API_KEY=<value>'.  Bearer tokens, OAuth tokens, and other secret forms are
out of scope — use a dedicated secret manager to prevent those from appearing
in tool output in the first place.
"""

from __future__ import annotations

import re

# Matches "api key <value>" with alphanumeric + common key chars
_API_KEY_RE = re.compile(r"(?i)(api[\s_-]*key[\s:=]+)[A-Za-z0-9\-_.+/]{8,}")


def redact(text: str) -> str:
    return _API_KEY_RE.sub(r"\1[REDACTED]", text)
