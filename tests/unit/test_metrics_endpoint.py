"""Unit tests for the GET /metrics HTTP endpoint."""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from src.mcp_relay.api.metrics_endpoint import metrics_handler
from src.mcp_relay.observability import metrics


@pytest.fixture(autouse=True)
def _reset():
    metrics.reset()
    yield
    metrics.reset()


@pytest.fixture()
def client():
    app = Starlette(routes=[Route("/metrics", metrics_handler)])
    return TestClient(app)


def test_metrics_returns_200(client):
    assert client.get("/metrics").status_code == 200


def test_metrics_returns_json(client):
    resp = client.get("/metrics")
    assert resp.headers["content-type"].startswith("application/json")


def test_metrics_has_counters_gauges_histograms_keys(client):
    data = client.get("/metrics").json()
    assert "counters" in data
    assert "gauges" in data
    assert "histograms" in data


def test_metrics_empty_when_no_activity(client):
    data = client.get("/metrics").json()
    assert data["counters"] == {}
    assert data["gauges"] == {}
    assert data["histograms"] == {}


def test_metrics_reflects_incremented_counter(client):
    metrics.increment("tool_calls_total", server="srv", tool="search")
    data = client.get("/metrics").json()
    assert data["counters"]["tool_calls_total{server=srv,tool=search}"] == 1


def test_metrics_reflects_gauge(client):
    metrics.gauge("tools_registered", 7.0, server="srv")
    data = client.get("/metrics").json()
    assert data["gauges"]["tools_registered{server=srv}"] == 7.0


def test_metrics_reflects_histogram(client):
    metrics.observe("tool_call_duration_seconds", 0.25, server="srv")
    data = client.get("/metrics").json()
    h = data["histograms"]["tool_call_duration_seconds{server=srv}"]
    assert h["count"] == 1
    assert abs(h["sum"] - 0.25) < 1e-9


def test_metrics_accumulates_across_requests(client):
    metrics.increment("tool_calls_total", server="srv", tool="t")
    metrics.increment("tool_calls_total", server="srv", tool="t")
    data = client.get("/metrics").json()
    assert data["counters"]["tool_calls_total{server=srv,tool=t}"] == 2


def test_metrics_snapshot_is_stable_between_requests(client):
    metrics.increment("simple")
    first = client.get("/metrics").json()
    second = client.get("/metrics").json()
    assert first == second
