"""Circuit breaker for LLM calls in the enrichment pipeline.

Trips after 3 consecutive failures, pauses for a configurable cooldown,
and records a metric each time it opens.
"""

from __future__ import annotations

import threading
import time as _time
from dataclasses import dataclass, field

from cases.observability import record_circuit_breaker_trip


@dataclass
class CircuitBreaker:
    """Stateful circuit breaker for a named LLM circuit.

    Attributes:
        name: Identifier used in metrics / logs (e.g. "source_llm").
        failure_threshold: Consecutive failures needed to trip (default 3).
        cooldown_seconds: How long the circuit stays open (default 60).
    """

    name: str
    failure_threshold: int = 3
    cooldown_seconds: float = 60.0

    _failure_count: int = field(default=0, repr=False)
    _last_failure_time: float = field(default=0.0, repr=False)
    _state: str = field(default="closed", repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def state(self) -> str:
        self._maybe_reset()
        return self._state

    def call(self, fn, *args, **kwargs):
        """Execute *fn(*args, **kwargs)* with circuit-breaker protection.

        Returns the function result on success.
        Raises ``CircuitBreakerOpenError`` if the circuit is open.
        Re-raises the original exception on failure after recording the failure.
        """
        with self._lock:
            self._maybe_reset()
            if self._state == "open":
                elapsed = _time.monotonic() - self._last_failure_time
                remaining = max(0.0, self.cooldown_seconds - elapsed)
                raise CircuitBreakerOpenError(
                    f"Circuit '{self.name}' is open. Retry in {remaining:.0f}s."
                )
        try:
            result = fn(*args, **kwargs)
        except Exception:
            self._record_failure()
            raise
        else:
            self._record_success()
            return result

    def _record_failure(self) -> None:
        with self._lock:
            self._failure_count += 1
            self._last_failure_time = _time.monotonic()
            if (
                self._state == "half-open"
                or self._failure_count >= self.failure_threshold
            ):
                was_closed = self._state != "open"
                self._state = "open"
                if was_closed:
                    record_circuit_breaker_trip(self.name)

    def _record_success(self) -> None:
        with self._lock:
            self._failure_count = 0
            self._state = "closed"

    def _maybe_reset(self) -> None:
        if self._state == "open":
            elapsed = _time.monotonic() - self._last_failure_time
            if elapsed >= self.cooldown_seconds:
                self._state = "half-open"
                self._failure_count = 0


class CircuitBreakerOpenError(Exception):
    """Raised when a call is attempted while the circuit is open."""
