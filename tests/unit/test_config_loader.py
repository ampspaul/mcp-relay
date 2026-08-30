"""Unit tests for config/loader.py — validate_servers, load_servers, load_security_policies."""

from __future__ import annotations

import textwrap

import pytest

from src.mcp_relay.config.loader import load_security_policies, load_servers, validate_servers

# ---------------------------------------------------------------------------
# validate_servers — required fields
# ---------------------------------------------------------------------------


def test_validate_requires_name():
    with pytest.raises(ValueError, match="name"):
        validate_servers([{"url": "https://example.com/mcp"}])


def test_validate_requires_url():
    with pytest.raises(ValueError, match="url"):
        validate_servers([{"name": "srv"}])


def test_validate_valid_minimal_server():
    validate_servers([{"name": "srv", "url": "https://example.com/mcp"}])


def test_validate_multiple_servers_all_valid():
    validate_servers(
        [
            {"name": "a", "url": "https://a.example.com/mcp"},
            {"name": "b", "url": "https://b.example.com/mcp"},
        ]
    )


def test_validate_empty_list_is_valid():
    validate_servers([])


# ---------------------------------------------------------------------------
# validate_servers — transport
# ---------------------------------------------------------------------------


def test_validate_default_transport_sse_is_valid():
    validate_servers([{"name": "srv", "url": "https://example.com/mcp", "transport": "sse"}])


def test_validate_streamable_http_transport_valid():
    validate_servers(
        [
            {
                "name": "srv",
                "url": "https://example.com/mcp",
                "transport": "streamable_http",
            }
        ]
    )


def test_validate_invalid_transport_raises():
    with pytest.raises(ValueError, match="transport"):
        validate_servers(
            [{"name": "srv", "url": "https://example.com/mcp", "transport": "websocket"}]
        )


# ---------------------------------------------------------------------------
# validate_servers — auth types
# ---------------------------------------------------------------------------


def test_validate_auth_none_is_valid():
    validate_servers([{"name": "srv", "url": "https://example.com/mcp", "auth": {"type": "none"}}])


def test_validate_auth_bearer_requires_value():
    with pytest.raises(ValueError, match="auth.value"):
        validate_servers(
            [{"name": "srv", "url": "https://example.com/mcp", "auth": {"type": "bearer"}}]
        )


def test_validate_auth_bearer_with_value_valid():
    validate_servers(
        [
            {
                "name": "srv",
                "url": "https://example.com/mcp",
                "auth": {"type": "bearer", "value": "secret::tok"},
            }
        ]
    )


def test_validate_auth_api_key_header_requires_value():
    with pytest.raises(ValueError, match="auth.value"):
        validate_servers(
            [
                {
                    "name": "srv",
                    "url": "https://example.com/mcp",
                    "auth": {"type": "api_key_header", "header_name": "X-API-Key"},
                }
            ]
        )


def test_validate_auth_invalid_type_raises():
    with pytest.raises(ValueError, match="auth.type"):
        validate_servers(
            [
                {
                    "name": "srv",
                    "url": "https://example.com/mcp",
                    "auth": {"type": "magic_token"},
                }
            ]
        )


def test_validate_oauth2_requires_token_url():
    with pytest.raises(ValueError, match="token_url"):
        validate_servers(
            [
                {
                    "name": "srv",
                    "url": "https://example.com/mcp",
                    "auth": {
                        "type": "oauth2_client_credentials",
                        "client_id": "id",
                        "client_secret": "secret::s",
                    },
                }
            ]
        )


def test_validate_oauth2_requires_client_id():
    with pytest.raises(ValueError, match="client_id"):
        validate_servers(
            [
                {
                    "name": "srv",
                    "url": "https://example.com/mcp",
                    "auth": {
                        "type": "oauth2_client_credentials",
                        "token_url": "https://auth.example.com/token",
                        "client_secret": "secret::s",
                    },
                }
            ]
        )


def test_validate_auth_bearer_value_zero_string_is_accepted():
    # auth.get("value") is None check — not falsy check — so "0" is a valid value.
    validate_servers(
        [
            {
                "name": "srv",
                "url": "https://example.com/mcp",
                "auth": {"type": "bearer", "value": "0"},
            }
        ]
    )


def test_validate_oauth2_all_required_fields_valid():
    validate_servers(
        [
            {
                "name": "srv",
                "url": "https://example.com/mcp",
                "auth": {
                    "type": "oauth2_client_credentials",
                    "token_url": "https://auth.example.com/token",
                    "client_id": "my-client",
                    "client_secret": "secret::s",
                },
            }
        ]
    )


# ---------------------------------------------------------------------------
# validate_servers — rate_limit
# ---------------------------------------------------------------------------


def test_validate_rate_limit_zero_is_valid():
    validate_servers(
        [
            {
                "name": "srv",
                "url": "https://example.com/mcp",
                "rate_limit": {"requests_per_day": 0},
            }
        ]
    )


def test_validate_rate_limit_positive_integer_valid():
    validate_servers(
        [
            {
                "name": "srv",
                "url": "https://example.com/mcp",
                "rate_limit": {"requests_per_day": 1000},
            }
        ]
    )


def test_validate_rate_limit_negative_raises():
    with pytest.raises(ValueError, match="requests_per_day"):
        validate_servers(
            [
                {
                    "name": "srv",
                    "url": "https://example.com/mcp",
                    "rate_limit": {"requests_per_day": -1},
                }
            ]
        )


def test_validate_rate_limit_string_raises():
    with pytest.raises(ValueError, match="requests_per_day"):
        validate_servers(
            [
                {
                    "name": "srv",
                    "url": "https://example.com/mcp",
                    "rate_limit": {"requests_per_day": "unlimited"},
                }
            ]
        )


# ---------------------------------------------------------------------------
# validate_servers — response_shape
# ---------------------------------------------------------------------------


def test_validate_response_shape_valid():
    validate_servers(
        [
            {
                "name": "srv",
                "url": "https://example.com/mcp",
                "response_shape": {"strip_nulls": True, "max_rows": 50},
            }
        ]
    )


def test_validate_response_shape_not_dict_raises():
    with pytest.raises(ValueError, match="response_shape"):
        validate_servers(
            [
                {
                    "name": "srv",
                    "url": "https://example.com/mcp",
                    "response_shape": "strip_nulls",
                }
            ]
        )


def test_validate_response_shape_unknown_key_raises():
    with pytest.raises(ValueError, match="unknown response_shape keys"):
        validate_servers(
            [
                {
                    "name": "srv",
                    "url": "https://example.com/mcp",
                    "response_shape": {"bogus": True},
                }
            ]
        )


def test_validate_error_label_includes_server_name():
    with pytest.raises(ValueError, match="my-server"):
        validate_servers(
            [{"name": "my-server", "url": "https://example.com/mcp", "transport": "bad"}]
        )


# ---------------------------------------------------------------------------
# load_servers — async YAML reading
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_servers_returns_empty_when_file_missing(tmp_path):
    result = await load_servers(tmp_path / "nonexistent.yaml")
    assert result == []


@pytest.mark.asyncio
async def test_load_servers_returns_empty_for_empty_file(tmp_path):
    cfg = tmp_path / "servers.yaml"
    cfg.write_text("")
    result = await load_servers(cfg)
    assert result == []


@pytest.mark.asyncio
async def test_load_servers_parses_server_list(tmp_path):
    cfg = tmp_path / "servers.yaml"
    cfg.write_text(
        textwrap.dedent("""\
        servers:
          - name: alpha
            url: https://alpha.example.com/mcp
          - name: beta
            url: https://beta.example.com/mcp
        """)
    )
    result = await load_servers(cfg)
    assert len(result) == 2
    assert result[0]["name"] == "alpha"
    assert result[1]["name"] == "beta"


@pytest.mark.asyncio
async def test_load_servers_no_servers_key_returns_empty(tmp_path):
    cfg = tmp_path / "servers.yaml"
    cfg.write_text("other_key: value\n")
    result = await load_servers(cfg)
    assert result == []


# ---------------------------------------------------------------------------
# load_security_policies — async YAML reading
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_security_policies_returns_empty_dict_when_missing(tmp_path):
    result = await load_security_policies(tmp_path / "nonexistent.yaml")
    assert result == {}


@pytest.mark.asyncio
async def test_load_security_policies_returns_empty_for_empty_file(tmp_path):
    p = tmp_path / "policies.yaml"
    p.write_text("")
    result = await load_security_policies(p)
    assert result == {}


@pytest.mark.asyncio
async def test_load_security_policies_parses_yaml(tmp_path):
    p = tmp_path / "policies.yaml"
    p.write_text(
        textwrap.dedent("""\
        tool_blocklist:
          - dangerous_tool
        tool_call_timeout_seconds: 45
        """)
    )
    result = await load_security_policies(p)
    assert result["tool_blocklist"] == ["dangerous_tool"]
    assert result["tool_call_timeout_seconds"] == 45
