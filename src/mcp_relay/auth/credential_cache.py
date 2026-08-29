"""In-memory credential cache with TTL for resolved auth credentials."""

from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

_credential_cache: dict[str, tuple[tuple[str, dict[str, str]], float]] = {}
_CREDENTIAL_TTL = 300  # 5 minutes


def get_cached(server_name: str) -> tuple[str, dict[str, str]] | None:
    now = time.time()
    if server_name in _credential_cache:
        (url, headers), expires_at = _credential_cache[server_name]
        if now < expires_at:
            logger.debug("[auth] %s: credentials served from cache", server_name)
            return url, dict(headers)
    return None


def set_cached(server_name: str, url: str, headers: dict[str, str]) -> None:
    _credential_cache[server_name] = ((url, headers), time.time() + _CREDENTIAL_TTL)


def invalidate(server_name: str | None = None) -> None:
    if server_name:
        _credential_cache.pop(server_name, None)
        logger.info("[auth] credential cache cleared for %s", server_name)
    else:
        _credential_cache.clear()
        logger.info("[auth] entire credential cache cleared")
