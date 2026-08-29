"""Unit tests for the dynamic tool refresh loop."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.mcp_relay.registry import server_registry

# ── helpers ──────────────────────────────────────────────────────────────────


class _Tool:
    def __init__(self, name: str, description: str = "", schema: dict | None = None):
        self.name = name
        self.description = description
        self.inputSchema = schema or {}


class _FakeMCP:
    """Minimal FastMCP stand-in with a mutable _tools dict."""

    def __init__(self):
        self._tool_manager = MagicMock()
        self._tool_manager._tools = {}
        self._added: list[str] = []

    def add_tool(self, fn, *, name, description=""):
        self._tool_manager._tools[name] = MagicMock()
        self._added.append(name)


def _cfg(name="srv", prefix="") -> dict:
    return {"name": name, "url": "https://example.com/mcp", "tool_prefix": prefix}


def _patch_discover(tools: list[_Tool]):
    return patch(
        "src.mcp_relay.registry.tool_refresher.discover",
        new=AsyncMock(return_value=tools),
    )


def _patch_load_servers(cfgs: list[dict]):
    return patch(
        "src.mcp_relay.registry.tool_refresher._load_servers",
        new=AsyncMock(return_value=cfgs),
    )


def _patch_policies(extra: dict | None = None):
    data = extra or {}
    return patch(
        "src.mcp_relay.registry.tool_refresher.load_security_policies",
        new=AsyncMock(return_value=data),
    )


@pytest.fixture(autouse=True)
def _clean_registered():
    server_registry._registered.clear()
    yield
    server_registry._registered.clear()


# ── _refresh_once: adding tools ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_refresh_adds_new_tool():
    mcp = _FakeMCP()
    cfg = _cfg()

    with _patch_load_servers([cfg]), _patch_policies(), _patch_discover([_Tool("get_price")]):
        from src.mcp_relay.registry.tool_refresher import _refresh_once

        await _refresh_once(mcp)

    assert "get_price" in mcp._tool_manager._tools
    assert "get_price" in server_registry._registered["srv"]


@pytest.mark.asyncio
async def test_refresh_skips_already_registered_tool():
    mcp = _FakeMCP()
    cfg = _cfg()
    server_registry._registered["srv"] = {"get_price"}
    mcp._tool_manager._tools["get_price"] = MagicMock()

    with _patch_load_servers([cfg]), _patch_policies(), _patch_discover([_Tool("get_price")]):
        from src.mcp_relay.registry.tool_refresher import _refresh_once

        await _refresh_once(mcp)

    assert mcp._added == []  # add_tool was not called


@pytest.mark.asyncio
async def test_refresh_applies_tool_prefix():
    mcp = _FakeMCP()
    cfg = _cfg(prefix="av_")

    with _patch_load_servers([cfg]), _patch_policies(), _patch_discover([_Tool("quote")]):
        from src.mcp_relay.registry.tool_refresher import _refresh_once

        await _refresh_once(mcp)

    assert "av_quote" in mcp._tool_manager._tools


# ── _refresh_once: removing tools ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_refresh_removes_stale_tool():
    mcp = _FakeMCP()
    cfg = _cfg()
    server_registry._registered["srv"] = {"old_tool", "current_tool"}
    mcp._tool_manager._tools["old_tool"] = MagicMock()
    mcp._tool_manager._tools["current_tool"] = MagicMock()

    with _patch_load_servers([cfg]), _patch_policies(), _patch_discover([_Tool("current_tool")]):
        from src.mcp_relay.registry.tool_refresher import _refresh_once

        await _refresh_once(mcp)

    assert "old_tool" not in mcp._tool_manager._tools
    assert "current_tool" in mcp._tool_manager._tools
    assert server_registry._registered["srv"] == {"current_tool"}


@pytest.mark.asyncio
async def test_refresh_removes_all_tools_when_upstream_empty():
    mcp = _FakeMCP()
    cfg = _cfg()
    server_registry._registered["srv"] = {"tool_a", "tool_b"}
    mcp._tool_manager._tools["tool_a"] = MagicMock()
    mcp._tool_manager._tools["tool_b"] = MagicMock()

    with _patch_load_servers([cfg]), _patch_policies(), _patch_discover([]):
        from src.mcp_relay.registry.tool_refresher import _refresh_once

        await _refresh_once(mcp)

    assert mcp._tool_manager._tools == {}
    assert server_registry._registered["srv"] == set()


# ── _refresh_once: blocklist ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_refresh_blocked_tool_not_added():
    mcp = _FakeMCP()
    cfg = _cfg()

    with (
        _patch_load_servers([cfg]),
        _patch_policies({"tool_blocklist": ["dangerous_tool"]}),
        _patch_discover([_Tool("dangerous_tool"), _Tool("safe_tool")]),
    ):
        from src.mcp_relay.registry.tool_refresher import _refresh_once

        await _refresh_once(mcp)

    assert "dangerous_tool" not in mcp._tool_manager._tools
    assert "safe_tool" in mcp._tool_manager._tools


@pytest.mark.asyncio
async def test_refresh_blocked_tool_removed_if_previously_registered():
    mcp = _FakeMCP()
    cfg = _cfg()
    server_registry._registered["srv"] = {"blocked_tool"}
    mcp._tool_manager._tools["blocked_tool"] = MagicMock()

    # upstream still returns the tool but blocklist now includes it
    with (
        _patch_load_servers([cfg]),
        _patch_policies({"tool_blocklist": ["blocked_tool"]}),
        _patch_discover([_Tool("blocked_tool")]),
    ):
        from src.mcp_relay.registry.tool_refresher import _refresh_once

        await _refresh_once(mcp)

    assert "blocked_tool" not in mcp._tool_manager._tools


# ── _refresh_once: per-server blocklist ──────────────────────────────────────


@pytest.mark.asyncio
async def test_refresh_per_server_blocklist_blocks_tool():
    mcp = _FakeMCP()
    cfg = {**_cfg(), "tool_blocklist": ["local_bad"]}

    with (
        _patch_load_servers([cfg]),
        _patch_policies(),
        _patch_discover([_Tool("local_bad"), _Tool("safe_tool")]),
    ):
        from src.mcp_relay.registry.tool_refresher import _refresh_once

        await _refresh_once(mcp)

    assert "local_bad" not in mcp._tool_manager._tools
    assert "safe_tool" in mcp._tool_manager._tools


@pytest.mark.asyncio
async def test_refresh_global_and_per_server_blocklists_combined():
    mcp = _FakeMCP()
    cfg = {**_cfg(), "tool_blocklist": ["local_bad"]}

    with (
        _patch_load_servers([cfg]),
        _patch_policies({"tool_blocklist": ["global_bad"]}),
        _patch_discover([_Tool("global_bad"), _Tool("local_bad"), _Tool("safe_tool")]),
    ):
        from src.mcp_relay.registry.tool_refresher import _refresh_once

        await _refresh_once(mcp)

    assert "global_bad" not in mcp._tool_manager._tools
    assert "local_bad" not in mcp._tool_manager._tools
    assert "safe_tool" in mcp._tool_manager._tools


# ── _refresh_once: disabled server ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_refresh_skips_disabled_server():
    mcp = _FakeMCP()
    cfg = {**_cfg(), "enabled": False}

    discover_mock = AsyncMock()
    with (
        _patch_load_servers([cfg]),
        _patch_policies(),
        patch("src.mcp_relay.registry.tool_refresher.discover", discover_mock),
    ):
        from src.mcp_relay.registry.tool_refresher import _refresh_once

        await _refresh_once(mcp)

    discover_mock.assert_not_called()


# ── _refresh_once: metrics ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_refresh_increments_added_metric():
    from src.mcp_relay.observability import metrics

    metrics.reset()
    mcp = _FakeMCP()

    with _patch_load_servers([_cfg()]), _patch_policies(), _patch_discover([_Tool("new_tool")]):
        from src.mcp_relay.registry.tool_refresher import _refresh_once

        await _refresh_once(mcp)

    snap = metrics.snapshot()
    added_key = next((k for k in snap.get("counters", {}) if "tools_added_total" in k), None)
    assert added_key is not None
    assert snap["counters"][added_key] == 1


@pytest.mark.asyncio
async def test_refresh_increments_removed_metric():
    from src.mcp_relay.observability import metrics

    metrics.reset()
    mcp = _FakeMCP()
    server_registry._registered["srv"] = {"gone_tool"}
    mcp._tool_manager._tools["gone_tool"] = MagicMock()

    with _patch_load_servers([_cfg()]), _patch_policies(), _patch_discover([]):
        from src.mcp_relay.registry.tool_refresher import _refresh_once

        await _refresh_once(mcp)

    snap = metrics.snapshot()
    removed_key = next((k for k in snap.get("counters", {}) if "tools_removed_total" in k), None)
    assert removed_key is not None
    assert snap["counters"][removed_key] == 1


# ── refresh_loop ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_refresh_loop_cancelled_cleanly():
    mcp = _FakeMCP()

    async def _never_discover(*_a, **_kw):
        return []

    with (
        _patch_load_servers([]),
        _patch_policies(),
        patch("src.mcp_relay.registry.tool_refresher.discover", _never_discover),
    ):
        from src.mcp_relay.registry.tool_refresher import refresh_loop

        task = asyncio.create_task(refresh_loop(mcp, interval_seconds=9999))
        await asyncio.sleep(0)  # let the task start
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_refresh_loop_survives_discovery_exception():
    mcp = _FakeMCP()
    call_count = 0

    async def _flaky(*_a, **_kw):
        nonlocal call_count
        call_count += 1
        raise RuntimeError("upstream down")

    with (
        patch(
            "src.mcp_relay.registry.tool_refresher._load_servers", AsyncMock(return_value=[_cfg()])
        ),
        _patch_policies(),
        patch("src.mcp_relay.registry.tool_refresher.discover", _flaky),
        patch("asyncio.sleep", new=AsyncMock(side_effect=[None, None, asyncio.CancelledError()])),
    ):
        import importlib

        from src.mcp_relay.registry import tool_refresher

        importlib.reload(tool_refresher)
        from src.mcp_relay.registry.tool_refresher import refresh_loop

        with pytest.raises(asyncio.CancelledError):
            await refresh_loop(mcp, interval_seconds=1)
