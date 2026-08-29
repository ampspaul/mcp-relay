"""Integration tests for remote tool calls.

Mocks at the transport layer so the full call_tool() pipeline
(rate limiting, session, response parsing) runs for real.
"""
from __future__ import annotations
import json
import pytest
from contextlib import asynccontextmanager
from unittest.mock import patch


# ── MCP mock objects ─────────────────────────────────────────────────────────

class _Content:
    def __init__(self, text=None, data=None):
        if text is not None:
            self.text = text
        if data is not None:
            self.data = data


class _CallResult:
    def __init__(self, content=None, is_error: bool = False):
        self.content = content or []
        self.isError = is_error


class _Session:
    def __init__(self, result: _CallResult, capture: list | None = None):
        self._result = result
        self._capture = capture  # records (tool_name, args) if provided

    async def initialize(self):
        pass

    async def call_tool(self, tool_name: str, arguments: dict):
        if self._capture is not None:
            self._capture.append((tool_name, arguments))
        return self._result


def _session_ctx(session: _Session):
    @asynccontextmanager
    async def _ctx(*_args, **_kwargs):
        yield session
    return _ctx


def _cfg(name: str = "test-srv", **flags) -> dict:
    return {"name": name, "url": "https://example.com/mcp", **flags}


# ── call_tool() response types ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_json_response_parsed_to_dict():
    payload = {"temperature": 72, "unit": "F"}
    session = _Session(_CallResult([_Content(text=json.dumps(payload))]))

    with patch("src.mcp_relay.registry.server_registry.open_session", _session_ctx(session)):
        from src.mcp_relay.registry.server_registry import call_tool
        result = await call_tool(_cfg(), "get_weather", {"city": "NYC"})

    assert result == payload


@pytest.mark.asyncio
async def test_plain_text_response_returned_as_string():
    session = _Session(_CallResult([_Content(text="Hello, world!")]))

    with patch("src.mcp_relay.registry.server_registry.open_session", _session_ctx(session)):
        from src.mcp_relay.registry.server_registry import call_tool
        result = await call_tool(_cfg(), "greet", {})

    assert result == "Hello, world!"


@pytest.mark.asyncio
async def test_empty_content_returns_empty_dict():
    session = _Session(_CallResult(content=[]))

    with patch("src.mcp_relay.registry.server_registry.open_session", _session_ctx(session)):
        from src.mcp_relay.registry.server_registry import call_tool
        result = await call_tool(_cfg(), "ping", {})

    assert result == {}


@pytest.mark.asyncio
async def test_data_content_returned_directly():
    binary = b"\x89PNG\r\n"
    session = _Session(_CallResult([_Content(data=binary)]))

    with patch("src.mcp_relay.registry.server_registry.open_session", _session_ctx(session)):
        from src.mcp_relay.registry.server_registry import call_tool
        result = await call_tool(_cfg(), "screenshot", {})

    assert result == binary


@pytest.mark.asyncio
async def test_is_error_true_raises_runtime_error():
    session = _Session(_CallResult([_Content(text="Tool not found")], is_error=True))

    with patch("src.mcp_relay.registry.server_registry.open_session", _session_ctx(session)):
        from src.mcp_relay.registry.server_registry import call_tool
        with pytest.raises(RuntimeError, match="failed"):
            await call_tool(_cfg(), "broken_tool", {})


@pytest.mark.asyncio
async def test_is_error_message_contains_tool_name():
    session = _Session(_CallResult([_Content(text="oops")], is_error=True))

    with patch("src.mcp_relay.registry.server_registry.open_session", _session_ctx(session)):
        from src.mcp_relay.registry.server_registry import call_tool
        with pytest.raises(RuntimeError, match="my_tool"):
            await call_tool(_cfg(), "my_tool", {})


@pytest.mark.asyncio
async def test_transport_exception_raises_runtime_error():
    @asynccontextmanager
    async def _failing(*_args, **_kwargs):
        raise ConnectionError("timeout")
        yield  # noqa: unreachable

    with patch("src.mcp_relay.registry.server_registry.open_session", _failing):
        from src.mcp_relay.registry.server_registry import call_tool
        with pytest.raises(RuntimeError, match="transport error"):
            await call_tool(_cfg(), "my_tool", {})


@pytest.mark.asyncio
async def test_arguments_forwarded_to_session():
    captured: list = []
    session = _Session(_CallResult([_Content(text="{}")]), capture=captured)

    with patch("src.mcp_relay.registry.server_registry.open_session", _session_ctx(session)):
        from src.mcp_relay.registry.server_registry import call_tool
        await call_tool(_cfg(), "search", {"query": "hello", "limit": 10})

    assert captured[0] == ("search", {"query": "hello", "limit": 10})


@pytest.mark.asyncio
async def test_rate_limit_response_signal_raises():
    payload = {"Note": "API rate limit reached. Please wait before retrying."}
    session = _Session(_CallResult([_Content(text=json.dumps(payload))]))
    cfg = _cfg(rate_limit={"response_signal_keys": ["Note"]})

    with patch("src.mcp_relay.registry.server_registry.open_session", _session_ctx(session)):
        from src.mcp_relay.registry.server_registry import call_tool
        with pytest.raises(RuntimeError, match="rate limit"):
            await call_tool(cfg, "get_quote", {})


@pytest.mark.asyncio
async def test_rate_limit_daily_quota_exceeded():
    from src.mcp_relay.resilience import rate_limiter
    rate_limiter._rate_counters.clear()

    session = _Session(_CallResult([_Content(text="{}")]))
    cfg = _cfg(rate_limit={"requests_per_day": 1})

    with patch("src.mcp_relay.registry.server_registry.open_session", _session_ctx(session)):
        from src.mcp_relay.registry.server_registry import call_tool
        await call_tool(cfg, "tool", {})   # first call — allowed
        with pytest.raises(RuntimeError, match="daily quota"):
            await call_tool(cfg, "tool", {})  # second call — blocked

    rate_limiter._rate_counters.clear()
