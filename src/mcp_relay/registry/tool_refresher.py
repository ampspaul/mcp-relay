"""Background loop that re-discovers tools from upstream servers at a fixed interval.

When an upstream MCP server adds or removes tools, the refresh loop picks up the
change without requiring a relay restart.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..config.loader import load_security_policies
from ..observability import metrics
from .proxy_builder import build
from .server_registry import (
    _POLICIES_PATH,
    _blocked,
    _load_servers,
    _parse_parameters,
    _registered,
    _tool_metadata,
    call_tool,
)
from .tool_discovery import discover

logger = logging.getLogger(__name__)


async def _refresh_once(mcp: Any) -> None:
    servers = await _load_servers()
    policies = await load_security_policies(_POLICIES_PATH)
    blocklist: set[str] = set(policies.get("tool_blocklist") or [])

    for cfg in servers:
        name = cfg["name"]
        if not cfg.get("enabled", True):
            continue

        prefix = cfg.get("tool_prefix", "")
        server_blocklist: set[str] = set(cfg.get("tool_blocklist") or [])
        effective_blocklist = blocklist | server_blocklist
        tools = await discover(cfg)

        # Build map of proxy_name -> upstream tool for this refresh cycle
        available: dict[str, Any] = {}
        newly_blocked: set[str] = set()
        for tool in tools:
            proxy_name = f"{prefix}{tool.name}" if prefix else tool.name
            if tool.name in effective_blocklist:
                newly_blocked.add(proxy_name)
                _tool_metadata[proxy_name] = {"description": tool.description or ""}
                continue
            available[proxy_name] = tool
        _blocked[name] = newly_blocked

        current = _registered.get(name, set())
        added = 0
        removed = 0

        # Add tools that appeared upstream since last registration
        for proxy_name, tool in available.items():
            if proxy_name not in current:
                schema: dict = tool.inputSchema or {}
                proxy_fn = build(cfg, tool.name, schema, call_tool)
                proxy_fn.__name__ = proxy_name
                proxy_fn.__qualname__ = proxy_name
                if mcp._tool_manager.get_tool(proxy_name) is None:
                    mcp.add_tool(proxy_fn, name=proxy_name, description=tool.description or "")
                    _tool_metadata[proxy_name] = {
                        "description": tool.description or "",
                        "parameters": _parse_parameters(schema),
                    }
                    current.add(proxy_name)
                    added += 1
                    metrics.increment("tools_added_total", server=name)
                    logger.info("[refresher] %s: added tool %r", name, proxy_name)

        # Remove tools that disappeared from upstream.
        # FastMCP 2.x has no public remove_tool() API; _tools is the backing
        # dict on ToolManager.  Pinned to fastmcp<3 in pyproject.toml.
        # When FastMCP exposes a public removal method, replace this line.
        stale = current - set(available.keys())
        for proxy_name in stale:
            mcp._tool_manager._tools.pop(proxy_name, None)  # noqa: SLF001
            _tool_metadata.pop(proxy_name, None)
            current.discard(proxy_name)
            removed += 1
            metrics.increment("tools_removed_total", server=name)
            logger.info("[refresher] %s: removed stale tool %r", name, proxy_name)

        _registered[name] = current
        metrics.gauge("tools_registered", float(len(current)), server=name)

        if added or removed:
            logger.info(
                "[refresher] %s: +%d added, -%d removed (total=%d)",
                name,
                added,
                removed,
                len(current),
            )


async def refresh_loop(mcp: Any, interval_seconds: int) -> None:
    """Run forever, refreshing tool registrations every `interval_seconds`."""
    logger.info("[refresher] background tool refresh started (interval=%ds)", interval_seconds)
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            metrics.increment("tool_refresh_total")
            await _refresh_once(mcp)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("[refresher] refresh cycle failed", exc_info=True)
