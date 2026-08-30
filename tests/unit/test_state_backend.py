"""Unit tests for the pluggable state backend."""

import pytest

from src.mcp_relay.resilience import state_backend
from src.mcp_relay.resilience.backends.memory import MemoryBackend


@pytest.fixture(autouse=True)
def _reset():
    state_backend.reset_backend()
    yield
    state_backend.reset_backend()


# ── MemoryBackend ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_memory_increment_returns_new_value():
    backend = MemoryBackend()
    assert await backend.increment("key") == 1
    assert await backend.increment("key") == 2
    assert await backend.increment("key") == 3


@pytest.mark.asyncio
async def test_memory_get_returns_zero_for_unknown_key():
    backend = MemoryBackend()
    assert await backend.get("missing") == 0


@pytest.mark.asyncio
async def test_memory_get_returns_current_count():
    backend = MemoryBackend()
    await backend.increment("k")
    await backend.increment("k")
    assert await backend.get("k") == 2


@pytest.mark.asyncio
async def test_memory_different_keys_independent():
    backend = MemoryBackend()
    await backend.increment("a")
    await backend.increment("a")
    await backend.increment("b")
    assert await backend.get("a") == 2
    assert await backend.get("b") == 1


@pytest.mark.asyncio
async def test_memory_ttl_argument_accepted():
    # Memory backend ignores TTL but must not raise
    backend = MemoryBackend()
    count = await backend.increment("k", ttl_seconds=3600)
    assert count == 1


# ── factory ───────────────────────────────────────────────────────────────────


def test_default_backend_is_memory(monkeypatch):
    monkeypatch.delenv("STATE_BACKEND", raising=False)
    backend = state_backend.get_backend()
    assert isinstance(backend, MemoryBackend)


def test_explicit_memory_backend(monkeypatch):
    monkeypatch.setenv("STATE_BACKEND", "memory")
    backend = state_backend.get_backend()
    assert isinstance(backend, MemoryBackend)


def test_unknown_backend_raises(monkeypatch):
    monkeypatch.setenv("STATE_BACKEND", "cassandra")
    with pytest.raises(ValueError, match="Unknown STATE_BACKEND"):
        state_backend.get_backend()


def test_singleton_returns_same_instance():
    b1 = state_backend.get_backend()
    b2 = state_backend.get_backend()
    assert b1 is b2


def test_reset_forces_new_instance():
    b1 = state_backend.get_backend()
    state_backend.reset_backend()
    b2 = state_backend.get_backend()
    assert b1 is not b2


def test_redis_backend_raises_without_package(monkeypatch):
    monkeypatch.setenv("STATE_BACKEND", "redis")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379")
    # redis package is not installed in the test env — expect RuntimeError
    import sys

    redis_modules = {k: v for k, v in sys.modules.items() if "redis" in k}
    for mod in redis_modules:
        monkeypatch.delitem(sys.modules, mod, raising=False)
    with pytest.raises((RuntimeError, ImportError)):
        state_backend.get_backend()
