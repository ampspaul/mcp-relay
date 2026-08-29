"""Unit tests for the circuit breaker."""

from __future__ import annotations

import asyncio
import time

import pytest

from src.mcp_relay.resilience.circuit_breaker import (
    CircuitBreaker,
    CircuitOpenError,
    State,
    get_circuit_breaker,
    reset_all,
)


@pytest.fixture(autouse=True)
def _isolate():
    reset_all()
    yield
    reset_all()


# ── basic construction ────────────────────────────────────────────────────────


def test_initial_state_is_closed():
    cb = CircuitBreaker("srv")
    assert cb.state == State.CLOSED


def test_initial_failure_count_is_zero():
    cb = CircuitBreaker("srv")
    assert cb.failure_count == 0


# ── CLOSED — happy path ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_closed_allows_successful_call():
    cb = CircuitBreaker("srv")

    async def ok():
        return 42

    result = await cb.call(ok())
    assert result == 42


@pytest.mark.asyncio
async def test_closed_successful_call_resets_failure_count():
    cb = CircuitBreaker("srv", failure_threshold=3)

    async def fail():
        raise ValueError("boom")

    async def ok():
        return "ok"

    # accumulate one failure then succeed
    with pytest.raises(ValueError):
        await cb.call(fail())
    assert cb.failure_count == 1
    await cb.call(ok())
    assert cb.failure_count == 0


@pytest.mark.asyncio
async def test_closed_failure_increments_count():
    cb = CircuitBreaker("srv", failure_threshold=5)

    async def fail():
        raise ValueError("x")

    with pytest.raises(ValueError):
        await cb.call(fail())
    assert cb.failure_count == 1


# ── CLOSED → OPEN transition ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_opens_after_failure_threshold():
    cb = CircuitBreaker("srv", failure_threshold=3)

    async def fail():
        raise ValueError("x")

    for _ in range(3):
        with pytest.raises((ValueError, CircuitOpenError)):
            await cb.call(fail())

    assert cb.state == State.OPEN


@pytest.mark.asyncio
async def test_exactly_at_threshold_opens():
    cb = CircuitBreaker("srv", failure_threshold=2)

    async def fail():
        raise RuntimeError("err")

    with pytest.raises(RuntimeError):
        await cb.call(fail())
    assert cb.state == State.CLOSED  # one failure, not open yet

    with pytest.raises(RuntimeError):
        await cb.call(fail())
    assert cb.state == State.OPEN


# ── OPEN — fast-fail behaviour ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_open_rejects_without_calling_upstream():
    cb = CircuitBreaker("srv", failure_threshold=1)
    called = []

    async def track():
        called.append(True)
        raise RuntimeError("err")

    with pytest.raises(RuntimeError):
        await cb.call(track())
    assert cb.state == State.OPEN

    with pytest.raises(CircuitOpenError):
        await cb.call(track())
    assert len(called) == 1  # second call never reached upstream


@pytest.mark.asyncio
async def test_open_raises_circuit_open_error():
    cb = CircuitBreaker("srv", failure_threshold=1)

    async def fail():
        raise RuntimeError("x")

    with pytest.raises(RuntimeError):
        await cb.call(fail())

    with pytest.raises(CircuitOpenError):
        await cb.call(fail())


# ── OPEN → HALF_OPEN transition ───────────────────────────────────────────────


def test_open_transitions_to_half_open_after_recovery_timeout():
    cb = CircuitBreaker("srv", failure_threshold=1, recovery_timeout_seconds=0.01)
    cb._state = State.OPEN
    cb._opened_at = time.monotonic() - 1.0  # fake elapsed time

    assert cb.state == State.HALF_OPEN


def test_open_does_not_transition_before_recovery_timeout():
    cb = CircuitBreaker("srv", failure_threshold=1, recovery_timeout_seconds=9999)
    cb._state = State.OPEN
    cb._opened_at = time.monotonic()

    assert cb.state == State.OPEN


# ── HALF_OPEN — probe behaviour ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_half_open_success_closes_circuit():
    cb = CircuitBreaker("srv", failure_threshold=1, recovery_timeout_seconds=0.01)

    async def ok():
        return "recovered"

    cb._state = State.OPEN
    cb._opened_at = time.monotonic() - 1.0

    result = await cb.call(ok())
    assert result == "recovered"
    assert cb.state == State.CLOSED


@pytest.mark.asyncio
async def test_half_open_failure_reopens_circuit():
    cb = CircuitBreaker("srv", failure_threshold=1, recovery_timeout_seconds=0.01)

    async def fail():
        raise RuntimeError("still broken")

    cb._state = State.OPEN
    cb._opened_at = time.monotonic() - 1.0
    assert cb.state == State.HALF_OPEN

    with pytest.raises(RuntimeError):
        await cb.call(fail())
    assert cb.state == State.OPEN


@pytest.mark.asyncio
async def test_half_open_requires_success_threshold():
    cb = CircuitBreaker(
        "srv", failure_threshold=1, recovery_timeout_seconds=0.01, success_threshold=2
    )

    async def ok():
        return "ok"

    cb._state = State.OPEN
    cb._opened_at = time.monotonic() - 1.0

    await cb.call(ok())
    assert cb.state == State.HALF_OPEN  # one success, need two

    await cb.call(ok())
    assert cb.state == State.CLOSED


# ── timeout handling ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_timeout_counts_as_failure():
    cb = CircuitBreaker("srv", failure_threshold=3)

    async def slow():
        await asyncio.sleep(10)

    with pytest.raises(asyncio.TimeoutError):
        await cb.call(slow(), timeout=0.01)

    assert cb.failure_count == 1


@pytest.mark.asyncio
async def test_timeout_can_open_circuit():
    cb = CircuitBreaker("srv", failure_threshold=2)

    async def slow():
        await asyncio.sleep(10)

    for _ in range(2):
        with pytest.raises((asyncio.TimeoutError, CircuitOpenError)):
            await cb.call(slow(), timeout=0.01)

    assert cb.state == State.OPEN


@pytest.mark.asyncio
async def test_no_timeout_when_none():
    cb = CircuitBreaker("srv")

    async def fast():
        return "quick"

    result = await cb.call(fast(), timeout=None)
    assert result == "quick"


# ── reset ─────────────────────────────────────────────────────────────────────


def test_reset_returns_to_closed():
    cb = CircuitBreaker("srv", failure_threshold=1)
    cb._state = State.OPEN
    cb._failure_count = 3
    cb.reset()

    assert cb.state == State.CLOSED
    assert cb.failure_count == 0


# ── singleton factory ─────────────────────────────────────────────────────────


def test_get_circuit_breaker_returns_same_instance():
    cb1 = get_circuit_breaker("srv")
    cb2 = get_circuit_breaker("srv")
    assert cb1 is cb2


def test_get_circuit_breaker_different_names_are_different():
    cb1 = get_circuit_breaker("srv_a")
    cb2 = get_circuit_breaker("srv_b")
    assert cb1 is not cb2


def test_get_circuit_breaker_applies_cfg_on_first_call():
    cb = get_circuit_breaker("srv", {"failure_threshold": 10, "recovery_timeout_seconds": 120})
    assert cb._failure_threshold == 10
    assert cb._recovery_timeout == 120.0


def test_get_circuit_breaker_ignores_cfg_on_subsequent_calls():
    cb1 = get_circuit_breaker("srv", {"failure_threshold": 10})
    cb2 = get_circuit_breaker("srv", {"failure_threshold": 99})  # ignored
    assert cb1 is cb2
    assert cb1._failure_threshold == 10


def test_get_circuit_breaker_defaults_when_no_cfg():
    cb = get_circuit_breaker("srv")
    assert cb._failure_threshold == 5
    assert cb._recovery_timeout == 60.0
    assert cb._success_threshold == 1


# ── reset_all isolation ───────────────────────────────────────────────────────


def test_reset_all_clears_registry():
    get_circuit_breaker("srv_a")
    get_circuit_breaker("srv_b")
    reset_all()
    cb = get_circuit_breaker("srv_a", {"failure_threshold": 99})
    assert cb._failure_threshold == 99  # freshly created, cfg applied


@pytest.mark.asyncio
async def test_reset_all_allows_reopening_with_fresh_config():
    cb_old = get_circuit_breaker("srv", {"failure_threshold": 1})

    async def fail():
        raise RuntimeError("x")

    with pytest.raises(RuntimeError):
        await cb_old.call(fail())
    assert cb_old.state == State.OPEN

    reset_all()
    cb_new = get_circuit_breaker("srv", {"failure_threshold": 100})
    assert cb_new.state == State.CLOSED
    assert cb_new._failure_threshold == 100


# ── exception propagation ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_original_exception_propagated():
    cb = CircuitBreaker("srv", failure_threshold=5)

    class CustomError(Exception):
        pass

    async def fail():
        raise CustomError("specific")

    with pytest.raises(CustomError, match="specific"):
        await cb.call(fail())


@pytest.mark.asyncio
async def test_circuit_open_error_not_double_counted():
    """CircuitOpenError from a nested call should not increment the failure count."""
    cb = CircuitBreaker("srv", failure_threshold=1)

    async def fail():
        raise RuntimeError("x")

    with pytest.raises(RuntimeError):
        await cb.call(fail())
    assert cb.state == State.OPEN

    count_before = cb.failure_count
    with pytest.raises(CircuitOpenError):
        await cb.call(fail())
    assert cb.failure_count == count_before  # not incremented again
