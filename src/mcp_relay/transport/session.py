"""MCP transport session management (SSE and StreamableHTTP)."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import httpx
from mcp.client.session import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamablehttp_client

from ..auth.resolver import resolve_connection

logger = logging.getLogger(__name__)

_CONNECT_TIMEOUT = 10.0
_READ_TIMEOUT = 30.0


def safe_exc_msg(exc: BaseException) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code} from upstream"
    if isinstance(exc, httpx.RequestError):
        return f"{type(exc).__name__} (connection-level error)"
    return repr(exc)


@asynccontextmanager
async def open_session(server_cfg: dict):
    name = server_cfg["name"]
    transport = server_cfg.get("transport", "sse")
    url, headers = await resolve_connection(server_cfg)

    logger.debug("[transport] %s: opening %s session", name, transport)
    try:
        if transport == "streamable_http":
            async with streamablehttp_client(
                url,
                headers=headers,
                timeout=_CONNECT_TIMEOUT,
                sse_read_timeout=_READ_TIMEOUT,
            ) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    logger.debug("[transport] %s: session initialised (streamable_http)", name)
                    yield session
        elif transport == "sse":
            async with sse_client(
                url,
                headers=headers,
                timeout=_CONNECT_TIMEOUT,
                sse_read_timeout=_READ_TIMEOUT,
            ) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    logger.debug("[transport] %s: session initialised (sse)", name)
                    yield session
        else:
            raise ValueError(
                f"[transport] {name!r}: unknown transport {transport!r} "
                "— expected 'sse' or 'streamable_http'"
            )
    except Exception as exc:
        logger.error(
            "[transport] %s: session error (transport=%s): %s", name, transport, safe_exc_msg(exc)
        )
        raise
