"""Integration tests for tool discovery and server registration.

Mocks at the transport layer (open_session) so the registry, discovery,
and proxy-builder logic all run for real.
"""
from __future__ import annotations
import pytest
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch


# ── MCP mock objects ─────────────────────────────────────────────────────────

class _Tool:
    def __init__(self, name: str, description: str = "", schema: dict | None = None):
        self.name = name
        self.description = description
        self.inputSchema = schema or {}


class _ListResult:
    def __init__(self, tools: list):
        self.tools = tools


class _Session:
    def __init__(self, tools: list):
        self._tools = tools

    async def initialize(self):
        pass

    async def list_tools(self):
        return _ListResult(self._tools)


def _session_ctx(session: _Session):
    @asynccontextmanager
    async def _ctx(*_args, **_kwargs):
        yield session
    return _ctx


# ── discover() ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_discover_returns_tool_list():
    tools = [_Tool("search"), _Tool("summarise")]
    session = _Session(tools)
    cfg = {"name": "test-srv", "url": "https://example.com/mcp"}

    with patch("src.mcp_relay.registry.tool_discovery.open_session", _session_ctx(session)):
        from src.mcp_relay.registry.tool_discovery import discover
        result = await discover(cfg)

    assert len(result) == 2
    assert result[0].name == "search"
    assert result[1].name == "summarise"


@pytest.mark.asyncio
async def test_discover_returns_empty_list_on_session_error():
    @asynccontextmanager
    async def _failing_ctx(*_args, **_kwargs):
        raise ConnectionError("unreachable")
        yield  # noqa: unreachable

    cfg = {"name": "bad-srv", "url": "https://bad.example.com/mcp"}
    with patch("src.mcp_relay.registry.tool_discovery.open_session", _failing_ctx):
        from src.mcp_relay.registry.tool_discovery import discover
        result = await discover(cfg)

    assert result == []


@pytest.mark.asyncio
async def test_discover_returns_empty_list_on_empty_server():
    session = _Session([])
    cfg = {"name": "empty-srv", "url": "https://example.com/mcp"}

    with patch("src.mcp_relay.registry.tool_discovery.open_session", _session_ctx(session)):
        from src.mcp_relay.registry.tool_discovery import discover
        result = await discover(cfg)

    assert result == []


# ── register_all() ───────────────────────────────────────────────────────────

def _mcp_mock():
    mcp = MagicMock()
    mcp.add_tool = MagicMock()
    return mcp


_EMPTY_POLICIES = AsyncMock(return_value={})


@pytest.mark.asyncio
async def test_register_all_adds_tools_to_mcp():
    tools = [_Tool("weather_current"), _Tool("weather_forecast")]
    cfg = [{"name": "weather", "url": "https://weather.example.com/mcp", "enabled": True}]
    mcp = _mcp_mock()

    with patch("src.mcp_relay.registry.server_registry._load_servers", AsyncMock(return_value=cfg)), \
         patch("src.mcp_relay.registry.server_registry.load_security_policies", _EMPTY_POLICIES), \
         patch("src.mcp_relay.registry.server_registry.discover", AsyncMock(return_value=tools)):
        from src.mcp_relay.registry.server_registry import register_all
        count = await register_all(mcp)

    assert count == 2
    assert mcp.add_tool.call_count == 2
    registered_names = {call.kwargs["name"] for call in mcp.add_tool.call_args_list}
    assert registered_names == {"weather_current", "weather_forecast"}


@pytest.mark.asyncio
async def test_register_all_applies_tool_prefix():
    tools = [_Tool("search"), _Tool("index")]
    cfg = [{"name": "elastic", "url": "https://es.example.com", "enabled": True, "tool_prefix": "es_"}]
    mcp = _mcp_mock()

    with patch("src.mcp_relay.registry.server_registry._load_servers", AsyncMock(return_value=cfg)), \
         patch("src.mcp_relay.registry.server_registry.load_security_policies", _EMPTY_POLICIES), \
         patch("src.mcp_relay.registry.server_registry.discover", AsyncMock(return_value=tools)):
        from src.mcp_relay.registry.server_registry import register_all
        count = await register_all(mcp)

    assert count == 2
    registered_names = {call.kwargs["name"] for call in mcp.add_tool.call_args_list}
    assert registered_names == {"es_search", "es_index"}


@pytest.mark.asyncio
async def test_register_all_skips_disabled_servers():
    cfg = [
        {"name": "active", "url": "https://active.example.com", "enabled": True},
        {"name": "inactive", "url": "https://inactive.example.com", "enabled": False},
    ]
    mcp = _mcp_mock()
    tools = [_Tool("do_thing")]

    async def _discover(server_cfg):
        return tools if server_cfg["name"] == "active" else []

    with patch("src.mcp_relay.registry.server_registry._load_servers", AsyncMock(return_value=cfg)), \
         patch("src.mcp_relay.registry.server_registry.load_security_policies", _EMPTY_POLICIES), \
         patch("src.mcp_relay.registry.server_registry.discover", side_effect=_discover):
        from src.mcp_relay.registry.server_registry import register_all
        count = await register_all(mcp)

    assert count == 1
    assert mcp.add_tool.call_count == 1


@pytest.mark.asyncio
async def test_register_all_skips_colliding_tool_names():
    tools_a = [_Tool("query")]
    tools_b = [_Tool("query")]   # same name — collision
    cfg = [
        {"name": "srv-a", "url": "https://a.example.com", "enabled": True},
        {"name": "srv-b", "url": "https://b.example.com", "enabled": True},
    ]
    mcp = _mcp_mock()

    async def _discover(server_cfg):
        return tools_a if server_cfg["name"] == "srv-a" else tools_b

    with patch("src.mcp_relay.registry.server_registry._load_servers", AsyncMock(return_value=cfg)), \
         patch("src.mcp_relay.registry.server_registry.load_security_policies", _EMPTY_POLICIES), \
         patch("src.mcp_relay.registry.server_registry.discover", side_effect=_discover):
        from src.mcp_relay.registry.server_registry import register_all
        count = await register_all(mcp)

    # Only the first "query" is registered; the second is skipped
    assert count == 1
    assert mcp.add_tool.call_count == 1


@pytest.mark.asyncio
async def test_register_all_raises_if_all_servers_return_no_tools():
    cfg = [{"name": "broken", "url": "https://broken.example.com", "enabled": True}]
    mcp = _mcp_mock()

    with patch("src.mcp_relay.registry.server_registry._load_servers", AsyncMock(return_value=cfg)), \
         patch("src.mcp_relay.registry.server_registry.load_security_policies", _EMPTY_POLICIES), \
         patch("src.mcp_relay.registry.server_registry.discover", AsyncMock(return_value=[])):
        from src.mcp_relay.registry.server_registry import register_all
        with pytest.raises(RuntimeError, match="startup failed"):
            await register_all(mcp)


@pytest.mark.asyncio
async def test_register_all_returns_zero_with_no_servers():
    mcp = _mcp_mock()

    with patch("src.mcp_relay.registry.server_registry._load_servers", AsyncMock(return_value=[])), \
         patch("src.mcp_relay.registry.server_registry.load_security_policies", _EMPTY_POLICIES):
        from src.mcp_relay.registry.server_registry import register_all
        count = await register_all(mcp)

    assert count == 0
    mcp.add_tool.assert_not_called()


@pytest.mark.asyncio
async def test_register_all_tool_description_forwarded():
    tools = [_Tool("do_thing", description="Does the thing", schema={})]
    cfg = [{"name": "srv", "url": "https://srv.example.com", "enabled": True}]
    mcp = _mcp_mock()

    with patch("src.mcp_relay.registry.server_registry._load_servers", AsyncMock(return_value=cfg)), \
         patch("src.mcp_relay.registry.server_registry.load_security_policies", _EMPTY_POLICIES), \
         patch("src.mcp_relay.registry.server_registry.discover", AsyncMock(return_value=tools)):
        from src.mcp_relay.registry.server_registry import register_all
        await register_all(mcp)

    call_kwargs = mcp.add_tool.call_args.kwargs
    assert call_kwargs["description"] == "Does the thing"


# ── tool blocklist ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_blocklist_skips_blocked_tool():
    tools = [_Tool("safe_tool"), _Tool("dangerous_tool")]
    cfg = [{"name": "srv", "url": "https://srv.example.com", "enabled": True}]
    mcp = _mcp_mock()
    policies = AsyncMock(return_value={"tool_blocklist": ["dangerous_tool"]})

    with patch("src.mcp_relay.registry.server_registry._load_servers", AsyncMock(return_value=cfg)), \
         patch("src.mcp_relay.registry.server_registry.load_security_policies", policies), \
         patch("src.mcp_relay.registry.server_registry.discover", AsyncMock(return_value=tools)):
        from src.mcp_relay.registry.server_registry import register_all
        count = await register_all(mcp)

    assert count == 1
    registered_names = {call.kwargs["name"] for call in mcp.add_tool.call_args_list}
    assert registered_names == {"safe_tool"}
    assert "dangerous_tool" not in registered_names


@pytest.mark.asyncio
async def test_blocklist_matches_original_tool_name_not_prefix():
    # prefix is applied after blocklist check — blocklist uses the upstream name
    tools = [_Tool("admin_reset"), _Tool("query")]
    cfg = [{"name": "srv", "url": "https://srv.example.com", "enabled": True, "tool_prefix": "fin_"}]
    mcp = _mcp_mock()
    policies = AsyncMock(return_value={"tool_blocklist": ["admin_reset"]})

    with patch("src.mcp_relay.registry.server_registry._load_servers", AsyncMock(return_value=cfg)), \
         patch("src.mcp_relay.registry.server_registry.load_security_policies", policies), \
         patch("src.mcp_relay.registry.server_registry.discover", AsyncMock(return_value=tools)):
        from src.mcp_relay.registry.server_registry import register_all
        count = await register_all(mcp)

    assert count == 1
    registered_names = {call.kwargs["name"] for call in mcp.add_tool.call_args_list}
    assert registered_names == {"fin_query"}


@pytest.mark.asyncio
async def test_blocklist_blocks_across_all_servers():
    tools_a = [_Tool("drop_table"), _Tool("read")]
    tools_b = [_Tool("drop_table"), _Tool("write")]
    cfg = [
        {"name": "srv-a", "url": "https://a.example.com", "enabled": True},
        {"name": "srv-b", "url": "https://b.example.com", "enabled": True},
    ]
    mcp = _mcp_mock()
    policies = AsyncMock(return_value={"tool_blocklist": ["drop_table"]})

    async def _discover(server_cfg):
        return tools_a if server_cfg["name"] == "srv-a" else tools_b

    with patch("src.mcp_relay.registry.server_registry._load_servers", AsyncMock(return_value=cfg)), \
         patch("src.mcp_relay.registry.server_registry.load_security_policies", policies), \
         patch("src.mcp_relay.registry.server_registry.discover", side_effect=_discover):
        from src.mcp_relay.registry.server_registry import register_all
        count = await register_all(mcp)

    assert count == 2
    registered_names = {call.kwargs["name"] for call in mcp.add_tool.call_args_list}
    assert registered_names == {"read", "write"}
    assert "drop_table" not in registered_names


@pytest.mark.asyncio
async def test_empty_blocklist_registers_all_tools():
    tools = [_Tool("tool_a"), _Tool("tool_b"), _Tool("tool_c")]
    cfg = [{"name": "srv", "url": "https://srv.example.com", "enabled": True}]
    mcp = _mcp_mock()
    policies = AsyncMock(return_value={"tool_blocklist": []})

    with patch("src.mcp_relay.registry.server_registry._load_servers", AsyncMock(return_value=cfg)), \
         patch("src.mcp_relay.registry.server_registry.load_security_policies", policies), \
         patch("src.mcp_relay.registry.server_registry.discover", AsyncMock(return_value=tools)):
        from src.mcp_relay.registry.server_registry import register_all
        count = await register_all(mcp)

    assert count == 3
