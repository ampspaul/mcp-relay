"""In-process memory state backend — resets on restart."""

from __future__ import annotations

from collections import defaultdict


class MemoryBackend:
    def __init__(self) -> None:
        self._counters: dict[str, int] = defaultdict(int)

    async def increment(self, key: str, ttl_seconds: int | None = None) -> int:
        self._counters[key] += 1
        return self._counters[key]

    async def get(self, key: str) -> int:
        return self._counters.get(key, 0)
