"""Unit tests for GET /registry endpoint."""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from src.mcp_relay.api.registry_endpoint import registry_handler
from src.mcp_relay.registry import server_registry
from src.mcp_relay.resilience.circuit_breaker import State, get_circuit_breaker, reset_all
from src.mcp_relay.transport import session_pool as _session_pool_mod


@pytest.fixture(autouse=True)
def _clean_state():
    server_registry._server_configs.clear()
    server_registry._registered.clear()
    server_registry._blocked.clear()
    server_registry._tool_metadata.clear()
    reset_all()
    _session_pool_mod._pool._sessions.clear()
    yield
    server_registry._server_configs.clear()
    server_registry._registered.clear()
    server_registry._blocked.clear()
    server_registry._tool_metadata.clear()
    reset_all()
    _session_pool_mod._pool._sessions.clear()


@pytest.fixture()
def client():
    app = Starlette(routes=[Route("/registry", registry_handler)])
    return TestClient(app)


def _meta(description="", parameters=None):
    return {"description": description, "parameters": parameters or []}


def _blocked_meta(description=""):
    return {"description": description}


# ── empty state ───────────────────────────────────────────────────────────────


def test_empty_registry_returns_200(client):
    assert client.get("/registry").status_code == 200


def test_empty_registry_shape(client):
    data = client.get("/registry").json()
    assert "servers" in data
    assert "summary" in data
    assert data["servers"] == []


def test_empty_summary_all_zeros(client):
    summary = client.get("/registry").json()["summary"]
    assert summary["total_servers"] == 0
    assert summary["enabled_servers"] == 0
    assert summary["available_tools"] == 0
    assert summary["blocked_tools"] == 0


# ── tool object shape ─────────────────────────────────────────────────────────


def test_available_tool_has_name_description_parameters(client):
    server_registry._server_configs.append({"name": "srv", "url": "https://example.com/mcp"})
    server_registry._registered["srv"] = {"get_quote"}
    server_registry._tool_metadata["get_quote"] = _meta(
        description="Get the latest stock quote.",
        parameters=[
            {"name": "symbol", "type": "string", "required": True, "description": "Ticker symbol"}
        ],
    )

    tool = client.get("/registry").json()["servers"][0]["tools"]["available"][0]
    assert tool["name"] == "get_quote"
    assert tool["description"] == "Get the latest stock quote."
    assert tool["parameters"] == [
        {"name": "symbol", "type": "string", "required": True, "description": "Ticker symbol"}
    ]


def test_blocked_tool_has_name_and_description_only(client):
    server_registry._server_configs.append({"name": "srv", "url": "https://example.com/mcp"})
    server_registry._blocked["srv"] = {"admin_reset"}
    server_registry._tool_metadata["admin_reset"] = _blocked_meta("Resets all admin state.")

    tool = client.get("/registry").json()["servers"][0]["tools"]["blocked"][0]
    assert tool["name"] == "admin_reset"
    assert tool["description"] == "Resets all admin state."
    assert "parameters" not in tool


def test_available_tool_with_no_metadata_returns_empty_defaults(client):
    server_registry._server_configs.append({"name": "srv", "url": "https://example.com/mcp"})
    server_registry._registered["srv"] = {"mystery_tool"}
    # no entry in _tool_metadata

    tool = client.get("/registry").json()["servers"][0]["tools"]["available"][0]
    assert tool["name"] == "mystery_tool"
    assert tool["description"] == ""
    assert tool["parameters"] == []


def test_available_tool_multiple_parameters(client):
    server_registry._server_configs.append({"name": "srv", "url": "https://example.com/mcp"})
    server_registry._registered["srv"] = {"search"}
    server_registry._tool_metadata["search"] = _meta(
        description="Search for results.",
        parameters=[
            {"name": "query", "type": "string", "required": True, "description": "Search query"},
            {"name": "limit", "type": "integer", "required": False, "description": "Max results"},
            {
                "name": "offset",
                "type": "integer",
                "required": False,
                "description": "Pagination offset",
            },
        ],
    )

    params = client.get("/registry").json()["servers"][0]["tools"]["available"][0]["parameters"]
    assert len(params) == 3
    assert params[0]["name"] == "query"
    assert params[0]["required"] is True
    assert params[1]["name"] == "limit"
    assert params[1]["required"] is False


# ── single server with available tools ───────────────────────────────────────


def test_server_listed_with_available_tools(client):
    server_registry._server_configs.append({"name": "srv", "url": "https://example.com/mcp"})
    server_registry._registered["srv"] = {"get_quote", "get_news"}

    srv = client.get("/registry").json()["servers"][0]
    assert srv["name"] == "srv"
    names = [t["name"] for t in srv["tools"]["available"]]
    assert sorted(names) == ["get_news", "get_quote"]
    assert srv["tools"]["blocked"] == []


def test_available_count_matches_tools(client):
    server_registry._server_configs.append({"name": "srv", "url": "https://example.com/mcp"})
    server_registry._registered["srv"] = {"a", "b", "c"}

    srv = client.get("/registry").json()["servers"][0]
    assert srv["counts"]["available"] == 3
    assert srv["counts"]["blocked"] == 0


# ── blocked tools ──────────────────────────────────────────────────────────────


def test_blocked_tools_listed(client):
    server_registry._server_configs.append({"name": "srv", "url": "https://example.com/mcp"})
    server_registry._registered["srv"] = {"safe_tool"}
    server_registry._blocked["srv"] = {"dangerous_tool"}

    srv = client.get("/registry").json()["servers"][0]
    assert srv["tools"]["available"][0]["name"] == "safe_tool"
    assert srv["tools"]["blocked"][0]["name"] == "dangerous_tool"


def test_blocked_count_in_summary(client):
    server_registry._server_configs.append({"name": "srv", "url": "https://example.com/mcp"})
    server_registry._registered["srv"] = {"safe_tool"}
    server_registry._blocked["srv"] = {"blocked_a", "blocked_b"}

    summary = client.get("/registry").json()["summary"]
    assert summary["blocked_tools"] == 2
    assert summary["available_tools"] == 1


def test_tool_blocked_and_available_names_are_mutually_exclusive(client):
    server_registry._server_configs.append({"name": "srv", "url": "https://example.com/mcp"})
    server_registry._registered["srv"] = {"tool_a"}
    server_registry._blocked["srv"] = {"tool_b"}

    srv = client.get("/registry").json()["servers"][0]
    available_names = {t["name"] for t in srv["tools"]["available"]}
    blocked_names = {t["name"] for t in srv["tools"]["blocked"]}
    assert available_names & blocked_names == set()


# ── multiple servers ───────────────────────────────────────────────────────────


def test_multiple_servers_listed(client):
    server_registry._server_configs.extend(
        [
            {"name": "srv_a", "url": "https://a.example.com/mcp"},
            {"name": "srv_b", "url": "https://b.example.com/mcp"},
        ]
    )
    server_registry._registered["srv_a"] = {"tool_1"}
    server_registry._registered["srv_b"] = {"tool_2", "tool_3"}

    data = client.get("/registry").json()
    assert len(data["servers"]) == 2
    assert data["summary"]["total_servers"] == 2
    assert data["summary"]["available_tools"] == 3


def test_summary_aggregates_blocked_across_servers(client):
    server_registry._server_configs.extend(
        [
            {"name": "srv_a", "url": "https://a.example.com/mcp"},
            {"name": "srv_b", "url": "https://b.example.com/mcp"},
        ]
    )
    server_registry._blocked["srv_a"] = {"bad_a"}
    server_registry._blocked["srv_b"] = {"bad_b1", "bad_b2"}

    assert client.get("/registry").json()["summary"]["blocked_tools"] == 3


# ── disabled servers ───────────────────────────────────────────────────────────


def test_disabled_server_appears_in_list(client):
    server_registry._server_configs.append(
        {"name": "srv", "url": "https://example.com/mcp", "enabled": False}
    )
    data = client.get("/registry").json()
    assert len(data["servers"]) == 1
    assert data["servers"][0]["enabled"] is False


def test_disabled_server_not_counted_in_enabled_servers(client):
    server_registry._server_configs.extend(
        [
            {"name": "active", "url": "https://a.example.com/mcp", "enabled": True},
            {"name": "inactive", "url": "https://b.example.com/mcp", "enabled": False},
        ]
    )
    summary = client.get("/registry").json()["summary"]
    assert summary["total_servers"] == 2
    assert summary["enabled_servers"] == 1


# ── safe fields only ──────────────────────────────────────────────────────────


def test_sensitive_fields_not_exposed(client):
    server_registry._server_configs.append(
        {
            "name": "srv",
            "url": "https://example.com/mcp",
            "headers": {"Authorization": "secret::MY_KEY"},
            "auth": {"type": "bearer", "token": "secret::TOKEN"},
        }
    )
    srv = client.get("/registry").json()["servers"][0]
    assert "headers" not in srv
    assert "auth" not in srv


def test_tool_prefix_exposed(client):
    server_registry._server_configs.append(
        {"name": "srv", "url": "https://example.com/mcp", "tool_prefix": "av_"}
    )
    server_registry._registered["srv"] = {"av_get_quote"}
    srv = client.get("/registry").json()["servers"][0]
    assert srv["tool_prefix"] == "av_"


# ── tool lists are sorted ─────────────────────────────────────────────────────


def test_available_tools_sorted_alphabetically(client):
    server_registry._server_configs.append({"name": "srv", "url": "https://example.com/mcp"})
    server_registry._registered["srv"] = {"zebra", "apple", "mango"}

    names = [t["name"] for t in client.get("/registry").json()["servers"][0]["tools"]["available"]]
    assert names == sorted(names)


def test_blocked_tools_sorted_alphabetically(client):
    server_registry._server_configs.append({"name": "srv", "url": "https://example.com/mcp"})
    server_registry._blocked["srv"] = {"zebra", "apple", "mango"}

    names = [t["name"] for t in client.get("/registry").json()["servers"][0]["tools"]["blocked"]]
    assert names == sorted(names)


# ── circuit breaker state ─────────────────────────────────────────────────────


def test_circuit_breaker_defaults_to_closed_when_no_breaker_exists(client):
    server_registry._server_configs.append({"name": "srv", "url": "https://example.com/mcp"})

    cb_info = client.get("/registry").json()["servers"][0]["circuit_breaker"]
    assert cb_info["state"] == "closed"
    assert cb_info["failure_count"] == 0


def test_circuit_breaker_reflects_open_state(client):
    server_registry._server_configs.append({"name": "srv", "url": "https://example.com/mcp"})
    cb = get_circuit_breaker("srv", {"failure_threshold": 1})
    cb._state = State.OPEN
    cb._failure_count = 3

    cb_info = client.get("/registry").json()["servers"][0]["circuit_breaker"]
    assert cb_info["state"] == "open"
    assert cb_info["failure_count"] == 3


def test_circuit_breaker_reflects_closed_state(client):
    server_registry._server_configs.append({"name": "srv", "url": "https://example.com/mcp"})
    get_circuit_breaker("srv")  # CLOSED by default

    cb_info = client.get("/registry").json()["servers"][0]["circuit_breaker"]
    assert cb_info["state"] == "closed"


def test_circuit_breaker_per_server_independent(client):
    server_registry._server_configs.extend(
        [
            {"name": "srv_a", "url": "https://a.example.com/mcp"},
            {"name": "srv_b", "url": "https://b.example.com/mcp"},
        ]
    )
    cb_a = get_circuit_breaker("srv_a")
    cb_a._state = State.OPEN

    data = client.get("/registry").json()["servers"]
    states = {s["name"]: s["circuit_breaker"]["state"] for s in data}
    assert states["srv_a"] == "open"
    assert states["srv_b"] == "closed"


# ── session pool status ───────────────────────────────────────────────────────


def test_session_status_not_started_when_pool_empty(client):
    server_registry._server_configs.append({"name": "srv", "url": "https://example.com/mcp"})

    session_info = client.get("/registry").json()["servers"][0]["session"]
    assert session_info["status"] == "not_started"


def test_session_status_connected_when_pool_has_session(client):
    from unittest.mock import MagicMock

    from src.mcp_relay.transport.session_pool import _ServerSession

    server_registry._server_configs.append({"name": "srv", "url": "https://example.com/mcp"})

    fake_srv = MagicMock(spec=_ServerSession)
    fake_srv.status = "connected"
    _session_pool_mod._pool._sessions["srv"] = fake_srv

    session_info = client.get("/registry").json()["servers"][0]["session"]
    assert session_info["status"] == "connected"


def test_session_status_connecting_during_reconnect(client):
    from unittest.mock import MagicMock

    from src.mcp_relay.transport.session_pool import _ServerSession

    server_registry._server_configs.append({"name": "srv", "url": "https://example.com/mcp"})

    fake_srv = MagicMock(spec=_ServerSession)
    fake_srv.status = "connecting"
    _session_pool_mod._pool._sessions["srv"] = fake_srv

    session_info = client.get("/registry").json()["servers"][0]["session"]
    assert session_info["status"] == "connecting"
