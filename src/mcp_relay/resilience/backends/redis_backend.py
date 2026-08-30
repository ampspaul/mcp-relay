"""Redis state backend — persists across restarts and works across multiple instances."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class RedisBackend:
    def __init__(self, url: str) -> None:
        try:
            from redis.asyncio import Redis  # type: ignore[import]
        except ImportError as exc:
            raise RuntimeError(
                "STATE_BACKEND=redis requires the redis package. "
                "Install it with:  pip install mcp-relay[redis]"
            ) from exc
        self._redis: Redis = Redis.from_url(url, decode_responses=True)

    async def increment(self, key: str, ttl_seconds: int | None = None) -> int:
        count = await self._redis.incr(key)
        # Set TTL only on first write to avoid resetting expiry on every call
        if count == 1 and ttl_seconds is not None:
            await self._redis.expire(key, ttl_seconds)
        return count

    async def get(self, key: str) -> int:
        val = await self._redis.get(key)
        return int(val) if val is not None else 0
