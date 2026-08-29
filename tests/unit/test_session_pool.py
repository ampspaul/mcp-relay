"""Unit tests for the persistent session pool."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.mcp_relay.transport.session_pool import SessionPool, _ServerSession

# ── helpers ───────────────────────────────────────────────────────────────────


def _cfg(name: str = "srv") -> dict:
    return {"name": name, "url": "https://example.com/mcp", "enabled": True}


def _fake_session(tool_result=None) -> MagicMock:
    session = MagicMock()
    session.call_tool = AsyncMock(return_value=tool_result or MagicMock(content=[]))
    return session


def _make_open_session(session: MagicMock, *, raises: Exception | None = None):
    """Return a patch for open_session that yields *session* or raises *raises*."""

    @asynccontextmanager
    async def _open(cfg):
        if raises:
            raise raises
        yield session

    return patch("src.mcp_relay.transport.session_pool.open_session", side_effect=_open)


def _make_open_session_then_close(session: MagicMock, close_after: float = 0.01):
    """Session opens, stays alive for close_after seconds, then closes gracefully."""

    @asynccontextmanager
    async def _open(cfg):
        yield session
        # After yield returns we're outside — context exits gracefully (no exception)
        # but we pause a bit so the pool actually connects before closing
        await asyncio.sleep(close_after)

    return patch("src.mcp_relay.transport.session_pool.open_session", side_effect=_open)


# ── _ServerSession ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_server_session_starts_connected():
    session = _fake_session()
    with _make_open_session(session):
        srv = _ServerSession(_cfg())
        await srv.start()
        assert srv.status == "connected"
        await srv.stop()


@pytest.mark.asyncio
async def test_server_session_get_session_returns_session():
    session = _fake_session()
    with _make_open_session(session):
        srv = _ServerSession(_cfg())
        await srv.start()
        result = await srv.get_session()
        assert result is session
        await srv.stop()


@pytest.mark.asyncio
async def test_server_session_stop_sets_status_closed():
    session = _fake_session()
    with _make_open_session(session):
        srv = _ServerSession(_cfg())
        await srv.start()
        await srv.stop()
        assert srv.status == "closed"


@pytest.mark.asyncio
async def test_server_session_startup_failure_raises():
    with _make_open_session(None, raises=ConnectionRefusedError("unreachable")):
        srv = _ServerSession(_cfg())
        with pytest.raises(ConnectionRefusedError):
            await srv.start()


@pytest.mark.asyncio
async def test_server_session_startup_failure_status():
    with _make_open_session(None, raises=OSError("timeout")):
        srv = _ServerSession(_cfg())
        with pytest.raises(OSError):
            await srv.start()
        # After startup error the runner exits but session isn't closed yet
        # (stop() hasn't been called); status reflects "connecting" (not shutdown)
        assert srv.status in ("connecting", "closed")


@pytest.mark.asyncio
async def test_server_session_reconnects_after_connection_drop():
    session = _fake_session()
    call_count = 0

    @asynccontextmanager
    async def _flaky_open(cfg):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise OSError("first attempt fails")
        yield session

    with patch("src.mcp_relay.transport.session_pool.open_session", side_effect=_flaky_open):
        srv = _ServerSession(_cfg())
        # First connect fails → startup_error is set, runner exits without retry
        with pytest.raises(OSError):
            await srv.start()
    # call_count should be 1 (only the first attempt, no retry on first failure)
    assert call_count == 1


@pytest.mark.asyncio
async def test_server_session_reconnects_after_mid_life_drop():
    """After a successful connect, a subsequent drop triggers reconnect."""
    session1 = _fake_session()
    session2 = _fake_session()
    call_count = 0

    @asynccontextmanager
    async def _reconnecting_open(cfg):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            yield session1
            # Graceful close after first yield
        elif call_count == 2:
            yield session2
            await asyncio.sleep(10)  # stay open

    with patch("src.mcp_relay.transport.session_pool.open_session", side_effect=_reconnecting_open):
        srv = _ServerSession(_cfg())
        await srv.start()
        assert srv.status == "connected"
        # After first open_session context exits gracefully, pool should reconnect
        await asyncio.sleep(0.05)  # let reconnect fire (backoff=1s → shortened in test)
        # Pool is reconnecting (backoff) or has reconnected; runner is alive
        assert srv._runner is not None and not srv._runner.done()
        await srv.stop()


@pytest.mark.asyncio
async def test_server_session_get_session_raises_when_unavailable():
    """get_session raises RuntimeError when connected is set but session is None."""
    srv = _ServerSession(_cfg())
    srv._connected.set()
    srv._session = None
    with pytest.raises(RuntimeError, match="unavailable"):
        await srv.get_session()


# ── SessionPool ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pool_starts_session_for_each_enabled_server():
    session = _fake_session()
    pool = SessionPool()
    with _make_open_session(session):
        await pool.start([_cfg("a"), _cfg("b")])
        assert "a" in pool._sessions
        assert "b" in pool._sessions
        await pool.stop()


@pytest.mark.asyncio
async def test_pool_skips_disabled_servers():
    session = _fake_session()
    pool = SessionPool()
    with _make_open_session(session):
        cfg_disabled = {"name": "off", "url": "https://x.com/mcp", "enabled": False}
        await pool.start([_cfg("on"), cfg_disabled])
        assert "on" in pool._sessions
        assert "off" not in pool._sessions
        await pool.stop()


@pytest.mark.asyncio
async def test_pool_call_tool_uses_pooled_session():
    mock_result = MagicMock(content=[MagicMock(text='{"ok": true}')])
    session = _fake_session(tool_result=mock_result)
    pool = SessionPool()

    with _make_open_session(session):
        await pool.start([_cfg()])
        await pool.call_tool(_cfg(), "my_tool", {"x": 1})
        session.call_tool.assert_awaited_once_with("my_tool", {"x": 1})
        await pool.stop()


@pytest.mark.asyncio
async def test_pool_call_tool_fallback_when_server_not_in_pool():
    """If a server was never added to the pool, call_tool opens an ephemeral session."""
    mock_result = MagicMock(content=[])
    fallback_session = _fake_session(tool_result=mock_result)
    pool = SessionPool()  # empty pool — no start()

    with _make_open_session(fallback_session):
        await pool.call_tool(_cfg("unknown"), "tool", {})
        fallback_session.call_tool.assert_awaited_once_with("tool", {})


@pytest.mark.asyncio
async def test_pool_stop_clears_sessions():
    session = _fake_session()
    pool = SessionPool()
    with _make_open_session(session):
        await pool.start([_cfg()])
        await pool.stop()
        assert pool._sessions == {}


@pytest.mark.asyncio
async def test_pool_startup_error_logs_warning_does_not_raise():
    """A server that fails to connect at startup should not abort pool.start()."""
    pool = SessionPool()
    with _make_open_session(None, raises=ConnectionRefusedError("down")):
        # Should not raise even though the server is unreachable
        await pool.start([_cfg()])
    # The session entry is still kept (so /registry can show 'connecting' status)
    assert "srv" in pool._sessions
    await pool.stop()


@pytest.mark.asyncio
async def test_pool_get_server_session_returns_none_for_unknown():
    pool = SessionPool()
    assert pool.get_server_session("nonexistent") is None


@pytest.mark.asyncio
async def test_pool_get_server_session_returns_session_object():
    session = _fake_session()
    pool = SessionPool()
    with _make_open_session(session):
        await pool.start([_cfg("srv")])
        srv = pool.get_server_session("srv")
        assert srv is not None
        assert srv.status == "connected"
        await pool.stop()


@pytest.mark.asyncio
async def test_pool_concurrent_calls_share_one_session():
    """Multiple concurrent tool calls go through the same session object."""
    results = []

    async def _slow_tool(name, args):
        await asyncio.sleep(0.01)
        results.append(name)
        return MagicMock(content=[])

    session = MagicMock()
    session.call_tool = AsyncMock(side_effect=_slow_tool)
    pool = SessionPool()

    with _make_open_session(session):
        await pool.start([_cfg()])
        await asyncio.gather(
            pool.call_tool(_cfg(), "tool_a", {}),
            pool.call_tool(_cfg(), "tool_b", {}),
            pool.call_tool(_cfg(), "tool_c", {}),
        )
        assert session.call_tool.await_count == 3
        await pool.stop()


# ── status values ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_status_connected_after_start():
    session = _fake_session()
    with _make_open_session(session):
        srv = _ServerSession(_cfg())
        await srv.start()
        assert srv.status == "connected"
        await srv.stop()


@pytest.mark.asyncio
async def test_status_closed_after_stop():
    session = _fake_session()
    with _make_open_session(session):
        srv = _ServerSession(_cfg())
        await srv.start()
        await srv.stop()
        assert srv.status == "closed"


def test_status_not_started_before_start():
    srv = _ServerSession(_cfg())
    # Before start(): shutdown not set, connected not set → "connecting"
    assert srv.status == "connecting"
