"""Per-server daily request rate limiting."""
from __future__ import annotations
import datetime
import logging
from typing import Any

logger = logging.getLogger(__name__)

_rate_counters: dict[str, tuple[datetime.date, int]] = {}


def check(server_cfg: dict) -> None:
    limit: int | None = (server_cfg.get("rate_limit") or {}).get("requests_per_day")
    if not limit:
        return
    name = server_cfg["name"]
    today = datetime.date.today()
    date, count = _rate_counters.get(name, (today, 0))
    if date != today:
        date, count = today, 0
    if count >= limit:
        raise RuntimeError(
            f"[{name}] daily quota of {limit} request(s) per day exhausted. "
            "Try again tomorrow or upgrade your API plan."
        )
    _rate_counters[name] = (today, count + 1)
    logger.debug("[resilience] %s: rate limit %d/%d used today", name, count + 1, limit)


def check_response(server_cfg: dict, parsed: Any) -> None:
    signal_keys: list[str] = (server_cfg.get("rate_limit") or {}).get("response_signal_keys", [])
    if not signal_keys or not isinstance(parsed, dict):
        return
    for key in signal_keys:
        if key in parsed:
            name = server_cfg["name"]
            msg = str(parsed[key])[:300]
            logger.warning("[resilience] %s: rate-limit signal detected (key=%r): %s", name, key, msg)
            raise RuntimeError(f"[{name}] rate limit reached — {msg}")
