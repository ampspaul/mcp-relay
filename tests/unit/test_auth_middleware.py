"""Unit tests for inbound bearer-token authentication middleware."""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from src.mcp_relay.middleware.authentication import BearerAuthMiddleware
from src.mcp_relay.observability import metrics


def _make_app(valid_tokens: set[str]) -> Starlette:
    def _endpoint(_: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    app = Starlette(
        routes=[
            Route("/health", _endpoint),
            Route("/metrics", _endpoint),
            Route("/sse", _endpoint),
            Route("/tools/call", _endpoint),
        ]
    )
    app.add_middleware(BearerAuthMiddleware, valid_tokens=valid_tokens)
    return app


@pytest.fixture(autouse=True)
def _reset_metrics():
    metrics.reset()
    yield
    metrics.reset()


# ── exempt paths ──────────────────────────────────────────────────────────────


def test_health_exempt_no_token():
    client = TestClient(_make_app({"secret"}), raise_server_exceptions=False)
    resp = client.get("/health")
    assert resp.status_code == 200


def test_metrics_requires_token():
    # /metrics is NOT exempt — it exposes operational data that can profile the deployment
    client = TestClient(_make_app({"secret"}), raise_server_exceptions=False)
    resp = client.get("/metrics")
    assert resp.status_code == 401


def test_metrics_allowed_with_valid_token():
    client = TestClient(_make_app({"secret"}), raise_server_exceptions=False)
    resp = client.get("/metrics", headers={"Authorization": "Bearer secret"})
    assert resp.status_code == 200


# ── no auth configured (type: none) ──────────────────────────────────────────


def test_empty_token_set_allows_all():
    client = TestClient(_make_app(set()), raise_server_exceptions=False)
    resp = client.get("/sse")
    assert resp.status_code == 200


# ── missing / malformed header ────────────────────────────────────────────────


def test_missing_auth_header_returns_401():
    client = TestClient(_make_app({"secret"}), raise_server_exceptions=False)
    resp = client.get("/sse")
    assert resp.status_code == 401


def test_missing_auth_header_has_www_authenticate():
    client = TestClient(_make_app({"secret"}), raise_server_exceptions=False)
    resp = client.get("/sse")
    assert resp.headers.get("www-authenticate") == "Bearer"


def test_malformed_header_not_bearer_returns_401():
    client = TestClient(_make_app({"secret"}), raise_server_exceptions=False)
    resp = client.get("/sse", headers={"Authorization": "Basic dXNlcjpwYXNz"})
    assert resp.status_code == 401


def test_missing_token_increments_rejected_counter():
    client = TestClient(_make_app({"secret"}), raise_server_exceptions=False)
    client.get("/sse")
    snap = metrics.snapshot()
    assert snap["counters"].get("inbound_auth_rejected_total{reason=missing_token}", 0) == 1


# ── invalid token ─────────────────────────────────────────────────────────────


def test_wrong_token_returns_401():
    client = TestClient(_make_app({"correct"}), raise_server_exceptions=False)
    resp = client.get("/sse", headers={"Authorization": "Bearer wrong"})
    assert resp.status_code == 401


def test_wrong_token_increments_rejected_counter():
    client = TestClient(_make_app({"correct"}), raise_server_exceptions=False)
    client.get("/sse", headers={"Authorization": "Bearer wrong"})
    snap = metrics.snapshot()
    assert snap["counters"].get("inbound_auth_rejected_total{reason=invalid_token}", 0) == 1


# ── valid token ───────────────────────────────────────────────────────────────


def test_valid_token_returns_200():
    client = TestClient(_make_app({"secret"}), raise_server_exceptions=False)
    resp = client.get("/sse", headers={"Authorization": "Bearer secret"})
    assert resp.status_code == 200


def test_valid_token_increments_accepted_counter():
    client = TestClient(_make_app({"secret"}), raise_server_exceptions=False)
    client.get("/sse", headers={"Authorization": "Bearer secret"})
    snap = metrics.snapshot()
    assert snap["counters"].get("inbound_auth_accepted_total", 0) == 1


def test_multiple_valid_tokens_all_accepted():
    client = TestClient(_make_app({"token-a", "token-b"}), raise_server_exceptions=False)
    assert client.get("/sse", headers={"Authorization": "Bearer token-a"}).status_code == 200
    assert client.get("/sse", headers={"Authorization": "Bearer token-b"}).status_code == 200


def test_valid_token_on_tool_call_path():
    client = TestClient(_make_app({"secret"}), raise_server_exceptions=False)
    resp = client.get("/tools/call", headers={"Authorization": "Bearer secret"})
    assert resp.status_code == 200


# ── response body ─────────────────────────────────────────────────────────────


def test_401_body_contains_error_detail():
    client = TestClient(_make_app({"secret"}), raise_server_exceptions=False)
    resp = client.get("/sse")
    body = resp.json()
    assert "error" in body
    assert "detail" in body
