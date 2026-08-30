"""Pluggable state backend for rate limit counters.

Selects the backend from the STATE_BACKEND environment variable:
  memory  — in-process dict; resets on restart (default)
  redis   — Redis via redis-py async; persists across restarts and instances
             requires REDIS_URL env var and  pip install mcp-relay[redis]
"""

from __future__ import annotations

import logging
import os
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)

_BACKEND_ENV = "STATE_BACKEND"
_DEFAULT = "memory"

_instance: StateBackend | None = None


@runtime_checkable
class StateBackend(Protocol):
    async def increment(self, key: str, ttl_seconds: int | None = None) -> int:
        """Atomically increment counter by 1, optionally setting TTL on first write.

        Returns the new value after increment.
        """
        ...

    async def get(self, key: str) -> int:
        """Return current counter value, or 0 if key does not exist."""
        ...


def get_backend() -> StateBackend:
    """Return the singleton backend, initialising it on first call."""
    global _instance
    if _instance is None:
        _instance = _create()
    return _instance


def _create() -> StateBackend:
    name = os.environ.get(_BACKEND_ENV, _DEFAULT).strip().lower()
    if name == "memory":
        from .backends.memory import MemoryBackend

        logger.info("[state] using memory backend (counters reset on restart)")
        return MemoryBackend()
    if name == "redis":
        from .backends.redis_backend import RedisBackend

        url = os.environ.get("REDIS_URL", "redis://localhost:6379")
        logger.info("[state] using Redis backend url=%s", url)
        return RedisBackend(url)
    raise ValueError(f"Unknown STATE_BACKEND {name!r}. Choose one of: memory, redis.")


def reset_backend() -> None:
    """Force re-initialisation on next call — used in tests."""
    global _instance
    _instance = None
