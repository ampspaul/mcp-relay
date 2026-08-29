"""Unit tests for the in-memory metrics module."""
from __future__ import annotations
import pytest
from src.mcp_relay.observability import metrics


@pytest.fixture(autouse=True)
def _reset():
    metrics.reset()
    yield
    metrics.reset()


# ── counters ──────────────────────────────────────────────────────────────────

def test_increment_creates_counter():
    metrics.increment("tool_calls_total", server="srv", tool="search")
    snap = metrics.snapshot()
    assert snap["counters"]["tool_calls_total{server=srv,tool=search}"] == 1


def test_increment_accumulates():
    for _ in range(5):
        metrics.increment("tool_calls_total", server="srv", tool="search")
    snap = metrics.snapshot()
    assert snap["counters"]["tool_calls_total{server=srv,tool=search}"] == 5


def test_different_labels_tracked_separately():
    metrics.increment("tool_errors_total", server="srv", type="transport")
    metrics.increment("tool_errors_total", server="srv", type="tool_error")
    snap = metrics.snapshot()
    assert snap["counters"]["tool_errors_total{server=srv,type=transport}"] == 1
    assert snap["counters"]["tool_errors_total{server=srv,type=tool_error}"] == 1


def test_no_labels_key_is_just_name():
    metrics.increment("simple_counter")
    snap = metrics.snapshot()
    assert snap["counters"]["simple_counter"] == 1


# ── gauges ────────────────────────────────────────────────────────────────────

def test_gauge_sets_value():
    metrics.gauge("tools_registered", 42.0, server="alpha_vantage")
    snap = metrics.snapshot()
    assert snap["gauges"]["tools_registered{server=alpha_vantage}"] == 42.0


def test_gauge_overwrites_previous_value():
    metrics.gauge("tools_registered", 10.0, server="srv")
    metrics.gauge("tools_registered", 133.0, server="srv")
    snap = metrics.snapshot()
    assert snap["gauges"]["tools_registered{server=srv}"] == 133.0


# ── histograms ────────────────────────────────────────────────────────────────

def test_observe_tracks_count_and_sum():
    metrics.observe("tool_call_duration_seconds", 0.1, server="srv")
    metrics.observe("tool_call_duration_seconds", 0.3, server="srv")
    snap = metrics.snapshot()
    h = snap["histograms"]["tool_call_duration_seconds{server=srv}"]
    assert h["count"] == 2
    assert abs(h["sum"] - 0.4) < 1e-9


def test_observe_tracks_min_and_max():
    metrics.observe("tool_call_duration_seconds", 0.5, server="srv")
    metrics.observe("tool_call_duration_seconds", 0.1, server="srv")
    metrics.observe("tool_call_duration_seconds", 0.9, server="srv")
    snap = metrics.snapshot()
    h = snap["histograms"]["tool_call_duration_seconds{server=srv}"]
    assert h["min"] == 0.1
    assert h["max"] == 0.9


def test_observe_computes_avg():
    metrics.observe("tool_call_duration_seconds", 1.0, server="srv")
    metrics.observe("tool_call_duration_seconds", 3.0, server="srv")
    snap = metrics.snapshot()
    h = snap["histograms"]["tool_call_duration_seconds{server=srv}"]
    assert h["avg"] == 2.0


# ── reset ─────────────────────────────────────────────────────────────────────

def test_reset_clears_all():
    metrics.increment("tool_calls_total", server="srv", tool="t")
    metrics.gauge("tools_registered", 5.0, server="srv")
    metrics.observe("tool_call_duration_seconds", 0.2, server="srv")
    metrics.reset()
    snap = metrics.snapshot()
    assert snap["counters"] == {}
    assert snap["gauges"] == {}
    assert snap["histograms"] == {}
