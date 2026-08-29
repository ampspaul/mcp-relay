"""Unit tests for the /health endpoint."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from src.mcp_relay.api.health import health
from src.mcp_relay.registry import server_registry
from src.mcp_relay.resilience.circuit_breaker import State, get_circuit_breaker, reset_all
from src.mcp_relay.transport import session_pool as _session_pool_mod


@pytest.fixture(autouse=True)
def _clean():
    server_registry._server_configs.clear()
    reset_all()
    _session_pool_mod._pool._sessions.clear()
    yield
    server_registry._server_configs.clear()
    reset_all()
    _session_pool_mod._pool._sessions.clear()


@pytest.fixture()
def client():
    app = Starlette(routes=[Route("/health", health)])
    return TestClient(app)


def _fake_srv(status: str) -> MagicMock:
    m = MagicMock()
    m.status = status
    return m


def _add_server(name: str, enabled: bool = True) -> None:
    server_registry._server_configs.append(
        {"name": name, "url": "https://example.com/mcp", "enabled": enabled}
    )


# ── basic shape ───────────────────────────────────────────────────────────────


def test_health_returns_200(client):
    assert client.get("/health").status_code == 200


def test_health_has_status_and_service(client):
    body = client.get("/health").json()
    assert "status" in body
    assert body["service"] == "mcp-relay"


def test_health_has_upstreams_list(client):
    body = client.get("/health").json()
    assert "upstreams" in body
    assert isinstance(body["upstreams"], list)


# ── no servers configured ─────────────────────────────────────────────────────


def test_no_servers_overall_status_ok(client):
    assert client.get("/health").json()["status"] == "ok"


def test_no_servers_upstreams_empty(client):
    assert client.get("/health").json()["upstreams"] == []


# ── single server — all ok ────────────────────────────────────────────────────


def test_single_ok_server_overall_ok(client):
    _add_server("srv")
    _session_pool_mod._pool._sessions["srv"] = _fake_srv("connected")
    # circuit breaker defaults to closed (no entry = closed)

    assert client.get("/health").json()["status"] == "ok"


def test_single_ok_server_upstream_status_ok(client):
    _add_server("srv")
    _session_pool_mod._pool._sessions["srv"] = _fake_srv("connected")

    upstream = client.get("/health").json()["upstreams"][0]
    assert upstream["name"] == "srv"
    assert upstream["status"] == "ok"
    assert upstream["session"] == "connected"
    assert upstream["circuit_breaker"] == "closed"


# ── session states ────────────────────────────────────────────────────────────


def test_session_connecting_reports_connecting(client):
    _add_server("srv")
    _session_pool_mod._pool._sessions["srv"] = _fake_srv("connecting")

    upstream = client.get("/health").json()["upstreams"][0]
    assert upstream["status"] == "connecting"
    assert upstream["session"] == "connecting"


def test_no_pool_session_reports_degraded(client):
    _add_server("srv")
    # No entry in pool — no_started → degraded

    upstream = client.get("/health").json()["upstreams"][0]
    assert upstream["status"] == "degraded"
    assert upstream["session"] == "not_started"


# ── circuit breaker states ────────────────────────────────────────────────────


def test_open_circuit_reports_degraded(client):
    _add_server("srv")
    _session_pool_mod._pool._sessions["srv"] = _fake_srv("connected")
    cb = get_circuit_breaker("srv")
    cb._state = State.OPEN

    upstream = client.get("/health").json()["upstreams"][0]
    assert upstream["status"] == "degraded"
    assert upstream["circuit_breaker"] == "open"


def test_half_open_circuit_reports_degraded(client):
    _add_server("srv")
    _session_pool_mod._pool._sessions["srv"] = _fake_srv("connected")
    cb = get_circuit_breaker("srv")
    cb._state = State.HALF_OPEN

    upstream = client.get("/health").json()["upstreams"][0]
    assert upstream["status"] == "degraded"
    assert upstream["circuit_breaker"] == "half_open"


def test_closed_circuit_connected_session_is_ok(client):
    _add_server("srv")
    _session_pool_mod._pool._sessions["srv"] = _fake_srv("connected")
    get_circuit_breaker("srv")  # creates it in CLOSED state

    upstream = client.get("/health").json()["upstreams"][0]
    assert upstream["status"] == "ok"


# ── disabled servers ──────────────────────────────────────────────────────────


def test_disabled_server_status_is_disabled(client):
    _add_server("srv", enabled=False)

    upstream = client.get("/health").json()["upstreams"][0]
    assert upstream["status"] == "disabled"


def test_disabled_server_not_counted_for_overall_status(client):
    _add_server("active")
    _add_server("inactive", enabled=False)
    _session_pool_mod._pool._sessions["active"] = _fake_srv("connected")

    assert client.get("/health").json()["status"] == "ok"


def test_all_disabled_servers_overall_ok(client):
    _add_server("srv", enabled=False)
    # No enabled servers → enabled_statuses is empty → overall is ok
    assert client.get("/health").json()["status"] == "ok"


# ── overall status aggregation ────────────────────────────────────────────────


def test_one_degraded_server_makes_overall_degraded(client):
    _add_server("healthy")
    _add_server("sick")
    _session_pool_mod._pool._sessions["healthy"] = _fake_srv("connected")
    _session_pool_mod._pool._sessions["sick"] = _fake_srv("connected")
    cb = get_circuit_breaker("sick")
    cb._state = State.OPEN

    assert client.get("/health").json()["status"] == "degraded"


def test_all_ok_servers_overall_ok(client):
    for name in ("srv_a", "srv_b", "srv_c"):
        _add_server(name)
        _session_pool_mod._pool._sessions[name] = _fake_srv("connected")

    assert client.get("/health").json()["status"] == "ok"


def test_one_connecting_server_makes_overall_degraded(client):
    _add_server("srv_a")
    _add_server("srv_b")
    _session_pool_mod._pool._sessions["srv_a"] = _fake_srv("connected")
    _session_pool_mod._pool._sessions["srv_b"] = _fake_srv("connecting")

    assert client.get("/health").json()["status"] == "degraded"


# ── upstream entry fields present ─────────────────────────────────────────────


def test_upstream_entry_has_required_fields(client):
    _add_server("srv")
    _session_pool_mod._pool._sessions["srv"] = _fake_srv("connected")

    upstream = client.get("/health").json()["upstreams"][0]
    assert "name" in upstream
    assert "status" in upstream
    assert "session" in upstream
    assert "circuit_breaker" in upstream


def test_disabled_upstream_entry_has_required_fields(client):
    _add_server("srv", enabled=False)

    upstream = client.get("/health").json()["upstreams"][0]
    assert "name" in upstream
    assert "status" in upstream
    assert "session" in upstream
    assert "circuit_breaker" in upstream
