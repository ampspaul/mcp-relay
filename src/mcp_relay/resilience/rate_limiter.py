"""Per-server daily request rate limiting."""

from __future__ import annotations

import datetime
import logging
from typing import Any

from ..observability import metrics
from .state_backend import get_backend

logger = logging.getLogger(__name__)


def _seconds_until_midnight() -> int:
    now = datetime.datetime.now()
    midnight = datetime.datetime.combine(now.date() + datetime.timedelta(days=1), datetime.time.min)
    return max(1, int((midnight - now).total_seconds()))


def _rate_key(server: str) -> str:
    return f"mcp_relay:ratelimit:{server}:{datetime.date.today()}"


async def check(server_cfg: dict) -> None:
    limit: int | None = (server_cfg.get("rate_limit") or {}).get("requests_per_day")
    if not limit:
        return

    name = server_cfg["name"]
    backend = get_backend()
    key = _rate_key(name)

    current = await backend.get(key)
    if current >= limit:
        metrics.increment("rate_limit_exceeded_total", server=name)
        raise RuntimeError(
            f"[{name}] daily quota of {limit} request(s) per day exhausted. "
            "Try again tomorrow or upgrade your API plan."
        )

    new_count = await backend.increment(key, ttl_seconds=_seconds_until_midnight())
    logger.debug("[resilience] %s: rate limit %d/%d used today", name, new_count, limit)


def check_response(server_cfg: dict, parsed: Any) -> None:
    signal_keys: list[str] = (server_cfg.get("rate_limit") or {}).get("response_signal_keys", [])
    if not signal_keys or not isinstance(parsed, dict):
        return
    for key in signal_keys:
        if key in parsed:
            name = server_cfg["name"]
            msg = str(parsed[key])[:300]
            logger.warning(
                "[resilience] %s: rate-limit signal detected (key=%r): %s", name, key, msg
            )
            metrics.increment("rate_limit_signal_total", server=name)
            raise RuntimeError(f"[{name}] rate limit reached — {msg}")
