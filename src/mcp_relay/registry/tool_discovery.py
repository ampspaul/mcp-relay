"""Connects to a remote MCP server and lists available tools."""

from __future__ import annotations

import logging

from ..transport.session import open_session

logger = logging.getLogger(__name__)


async def discover(server_cfg: dict) -> list:
    name = server_cfg["name"]
    logger.info("[registry] %s: discovering tools...", name)
    try:
        async with open_session(server_cfg) as session:
            result = await session.list_tools()
        logger.info("[registry] %s: discovered %d tool(s)", name, len(result.tools))
        return result.tools
    except Exception as exc:
        logger.error("[registry] %s: tool discovery failed — %s", name, exc, exc_info=True)
        return []
