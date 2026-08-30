"""Persistent per-server MCP session pool.

Maintains one long-lived ClientSession per upstream server instead of opening
and closing a new session for every tool call. When a connection drops the
session is automatically re-established with exponential backoff.

Usage
-----
Start from the app lifespan (after register_all has populated _server_configs):

    await _pool.start(enabled_server_configs)
    ...
    await _pool.stop()

Tool calls then use the pool:

    result = await _pool.call_tool(server_cfg, tool_name, arguments)
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .session import open_session, safe_exc_msg

logger = logging.getLogger(__name__)

_INITIAL_BACKOFF = 1.0
_MAX_BACKOFF = 60.0


class _ServerSession:
    """Holds one persistent MCP ClientSession with automatic reconnect."""

    def __init__(self, server_cfg: dict) -> None:
        self._cfg = server_cfg
        self._name: str = server_cfg["name"]
        self._session: Any | None = None  # mcp.client.session.ClientSession
        self._connected = asyncio.Event()
        self._shutdown = asyncio.Event()
        self._runner: asyncio.Task | None = None
        self._startup_error: Exception | None = None

    # ── public API ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Connect and wait until the session is ready (or startup fails)."""
        self._runner = asyncio.create_task(self._connection_loop(), name=f"pool-{self._name}")
        await self._connected.wait()
        if self._startup_error is not None:
            raise self._startup_error

    async def stop(self) -> None:
        """Shut down the persistent session cleanly."""
        self._shutdown.set()
        if self._runner and not self._runner.done():
            self._runner.cancel()
            try:
                await self._runner
            except asyncio.CancelledError:
                pass
        self._session = None

    async def get_session(self) -> Any:
        """Return the active ClientSession, blocking briefly while (re)connecting."""
        await self._connected.wait()
        if self._session is None:
            raise RuntimeError(f"[pool] {self._name}: session unavailable")
        return self._session

    @property
    def status(self) -> str:
        if self._shutdown.is_set():
            return "closed"
        if self._connected.is_set() and self._session is not None:
            return "connected"
        return "connecting"

    # ── connection loop ───────────────────────────────────────────────────────

    async def _connection_loop(self) -> None:
        first = True
        backoff = _INITIAL_BACKOFF

        while not self._shutdown.is_set():
            try:
                async with open_session(self._cfg) as session:
                    self._session = session
                    self._startup_error = None
                    self._connected.set()
                    first = False
                    backoff = _INITIAL_BACKOFF
                    logger.info("[pool] %s: session connected", self._name)
                    # Hold the session open until shutdown is requested or the
                    # connection drops (open_session exits or raises).
                    await self._shutdown.wait()

                # open_session exited without exception (graceful server close).
                if not self._shutdown.is_set():
                    self._session = None
                    self._connected.clear()
                    logger.warning(
                        "[pool] %s: connection closed by server — reconnecting", self._name
                    )
                    await self._sleep_backoff(backoff)
                    backoff = min(backoff * 2, _MAX_BACKOFF)

            except asyncio.CancelledError:
                break

            except Exception as exc:
                self._session = None
                msg = safe_exc_msg(exc)

                if first:
                    # Surface startup errors to start() so the caller decides
                    # whether to abort or ignore.
                    self._startup_error = exc
                    self._connected.set()
                    logger.error("[pool] %s: initial connection failed: %s", self._name, msg)
                    return

                self._connected.clear()
                logger.warning(
                    "[pool] %s: session lost (%s) — reconnecting in %.0fs",
                    self._name,
                    msg,
                    backoff,
                )
                await self._sleep_backoff(backoff)
                backoff = min(backoff * 2, _MAX_BACKOFF)

        self._session = None

    async def _sleep_backoff(self, seconds: float) -> None:
        """Sleep for *seconds* but wake immediately if shutdown is requested."""
        try:
            await asyncio.wait_for(self._shutdown.wait(), timeout=seconds)
        except TimeoutError:
            pass


class SessionPool:
    """Module-level pool — one _ServerSession per enabled upstream server."""

    def __init__(self) -> None:
        self._sessions: dict[str, _ServerSession] = {}

    async def start(self, server_configs: list[dict]) -> None:
        """Open persistent sessions for all enabled servers.

        Startup errors for individual servers are logged as warnings rather than
        raising, because the relay may be healthy enough to serve other servers.
        The circuit breaker and health endpoint surface per-server problems.
        """
        for cfg in server_configs:
            if not cfg.get("enabled", True):
                continue
            name = cfg["name"]
            srv = _ServerSession(cfg)
            try:
                await srv.start()
                self._sessions[name] = srv
            except Exception as exc:
                logger.warning(
                    "[pool] %s: could not establish persistent session (%s) — "
                    "calls will open ephemeral sessions as fallback",
                    name,
                    safe_exc_msg(exc),
                )
                # Keep the _ServerSession so status is visible in /registry,
                # but it will be in 'connecting' state until reconnect succeeds.
                self._sessions[name] = srv

    async def stop(self) -> None:
        """Close all pooled sessions."""
        for name, srv in list(self._sessions.items()):
            try:
                await srv.stop()
            except Exception:
                logger.debug("[pool] %s: error during stop (ignored)", name)
        self._sessions.clear()

    async def call_tool(self, server_cfg: dict, tool_name: str, arguments: dict) -> Any:
        """Call *tool_name* on *server_cfg* using the pooled session.

        Falls back to an ephemeral session if no pooled session exists for this
        server (e.g. the pool was not started, or startup failed and reconnect
        hasn't succeeded yet).
        """
        name = server_cfg["name"]
        srv = self._sessions.get(name)
        if srv is not None:
            session = await srv.get_session()
            return await session.call_tool(tool_name, arguments)

        # Fallback: open a short-lived session (pre-pool behaviour).
        logger.debug("[pool] %s: no pooled session — opening ephemeral session", name)
        async with open_session(server_cfg) as session:
            return await session.call_tool(tool_name, arguments)

    def get_server_session(self, server_name: str) -> _ServerSession | None:
        return self._sessions.get(server_name)


# Module-level singleton — imported by server_registry and main.
_pool = SessionPool()
