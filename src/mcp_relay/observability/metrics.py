"""In-memory metrics — counters and histograms exposed via /metrics."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

_counters: dict[str, int] = defaultdict(int)
_gauges: dict[str, float] = {}
_histograms: dict[str, dict[str, float]] = {}


def _key(name: str, labels: dict[str, str]) -> str:
    if not labels:
        return name
    pairs = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
    return f"{name}{{{pairs}}}"


def increment(name: str, **labels: str) -> None:
    _counters[_key(name, labels)] += 1


def gauge(name: str, value: float, **labels: str) -> None:
    _gauges[_key(name, labels)] = value


def observe(name: str, value: float, **labels: str) -> None:
    key = _key(name, labels)
    if key not in _histograms:
        _histograms[key] = {"count": 0.0, "sum": 0.0, "min": value, "max": value}
    h = _histograms[key]
    h["count"] += 1
    h["sum"] += value
    h["min"] = min(h["min"], value)
    h["max"] = max(h["max"], value)


def snapshot() -> dict[str, Any]:
    counters = dict(_counters)
    gauges = dict(_gauges)
    histograms = {k: {**v, "avg": round(v["sum"] / v["count"], 4)} for k, v in _histograms.items()}
    return {"counters": counters, "gauges": gauges, "histograms": histograms}


def reset() -> None:
    """Clear all metrics — used in tests."""
    _counters.clear()
    _gauges.clear()
    _histograms.clear()
