"""Per-server circuit breaker to stop cascading failures from slow or broken upstream servers.

States:
  CLOSED   — normal operation; failures are counted.
  OPEN     — threshold exceeded; calls are rejected immediately without hitting upstream.
  HALF_OPEN — recovery window elapsed; one probe is allowed through to test the server.
               Success → CLOSED.  Failure → OPEN (reset timer).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Coroutine
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)


class State(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(RuntimeError):
    """Raised when the circuit is open and a call is rejected without reaching upstream."""


class CircuitBreaker:
    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        recovery_timeout_seconds: float = 60.0,
        success_threshold: int = 1,
    ) -> None:
        self.name = name
        self._failure_threshold = failure_threshold
        self._recovery_timeout = recovery_timeout_seconds
        self._success_threshold = success_threshold
        self._state = State.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._opened_at: float | None = None

    @property
    def state(self) -> State:
        if self._state == State.OPEN and self._opened_at is not None:
            if time.monotonic() - self._opened_at >= self._recovery_timeout:
                logger.info(
                    "[circuit_breaker] %s: OPEN → HALF_OPEN (%.0fs recovery elapsed)",
                    self.name,
                    self._recovery_timeout,
                )
                self._state = State.HALF_OPEN
                self._success_count = 0
        return self._state

    @property
    def failure_count(self) -> int:
        return self._failure_count

    async def call(
        self,
        coro: Coroutine[Any, Any, Any],
        *,
        timeout: float | None = None,
    ) -> Any:
        """Execute *coro*, applying timeout and circuit-breaker logic.

        Raises:
            CircuitOpenError: circuit is OPEN — call rejected without hitting upstream.
            asyncio.TimeoutError: call exceeded *timeout* seconds (counts as a failure).
            Any exception raised by *coro* (also counts as a failure).
        """
        current = self.state
        if current == State.OPEN:
            raise CircuitOpenError(
                f"[{self.name}] circuit breaker OPEN — upstream unavailable. "
                f"Retry after {self._recovery_timeout:.0f}s."
            )

        try:
            if timeout is not None:
                result = await asyncio.wait_for(coro, timeout=timeout)
            else:
                result = await coro
            self._on_success()
            return result
        except TimeoutError:
            self._on_failure()
            raise
        except CircuitOpenError:
            raise
        except Exception:
            self._on_failure()
            raise

    def _on_success(self) -> None:
        if self._state == State.HALF_OPEN:
            self._success_count += 1
            if self._success_count >= self._success_threshold:
                logger.info(
                    "[circuit_breaker] %s: HALF_OPEN → CLOSED (upstream recovered)", self.name
                )
                self._state = State.CLOSED
                self._failure_count = 0
        else:
            self._failure_count = 0

    def _on_failure(self) -> None:
        self._failure_count += 1
        if self._state == State.HALF_OPEN or self._failure_count >= self._failure_threshold:
            if self._state != State.OPEN:
                logger.warning(
                    "[circuit_breaker] %s: → OPEN after %d failure(s)",
                    self.name,
                    self._failure_count,
                )
            self._state = State.OPEN
            self._opened_at = time.monotonic()

    def reset(self) -> None:
        """Force the breaker back to CLOSED — used in tests."""
        self._state = State.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._opened_at = None


# ── module-level registry — one breaker per server name ──────────────────────

_breakers: dict[str, CircuitBreaker] = {}


def get_circuit_breaker(name: str, cfg: dict | None = None) -> CircuitBreaker:
    """Return the CircuitBreaker for *name*, creating it from *cfg* on first call.

    *cfg* keys (all optional):
        failure_threshold (int, default 5)
        recovery_timeout_seconds (float, default 60)
        success_threshold (int, default 1)
    """
    if name not in _breakers:
        c = cfg or {}
        _breakers[name] = CircuitBreaker(
            name=name,
            failure_threshold=int(c.get("failure_threshold", 5)),
            recovery_timeout_seconds=float(c.get("recovery_timeout_seconds", 60.0)),
            success_threshold=int(c.get("success_threshold", 1)),
        )
    return _breakers[name]


def reset_all() -> None:
    """Clear all circuit breakers — for test isolation."""
    _breakers.clear()
