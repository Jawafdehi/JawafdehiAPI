"""Tests for circuit breaker module."""

import pytest

from cases import circuit_breaker as _cb


def _raise_runtime_error():
    raise RuntimeError("boom")


@pytest.fixture(autouse=True)
def _mock_clock(monkeypatch):
    """Replace _time.monotonic so tests control time without wall-clock sleeps."""
    t = 0.0

    def monotonic():
        return t

    monkeypatch.setattr(_cb._time, "monotonic", monotonic)

    def advance(seconds):
        nonlocal t
        t += seconds

    monkeypatch.setattr(_cb, "_mock_advance", advance, raising=False)


def _advance(seconds):
    """Advance the mocked monotonic clock."""
    if hasattr(_cb, "_mock_advance"):
        _cb._mock_advance(seconds)


class TestCircuitBreaker:
    def test_closed_on_init(self):
        cb = _cb.CircuitBreaker(name="test")
        assert cb.state == "closed"

    def test_passes_successful_calls(self):
        cb = _cb.CircuitBreaker(name="test")
        result = cb.call(lambda x: x * 2, 21)
        assert result == 42
        assert cb.state == "closed"

    def test_trips_after_consecutive_failures(self):
        cb = _cb.CircuitBreaker(name="test")
        for _ in range(3):
            try:
                cb.call(_raise_runtime_error)
            except RuntimeError:
                pass
        assert cb.state == "open"

    def test_raises_open_error_when_open(self):
        cb = _cb.CircuitBreaker(name="test", failure_threshold=1)
        try:
            cb.call(_raise_runtime_error)
        except RuntimeError:
            pass
        assert cb.state == "open"
        with pytest.raises(_cb.CircuitBreakerOpenError, match="open"):
            cb.call(lambda: 42)

    def test_resets_after_cooldown(self):
        cb = _cb.CircuitBreaker(name="test", failure_threshold=1, cooldown_seconds=0.01)
        try:
            cb.call(_raise_runtime_error)
        except RuntimeError:
            pass
        assert cb.state == "open"
        _advance(0.02)
        result = cb.call(lambda: 99)
        assert result == 99
        assert cb.state == "closed"

    def test_half_open_success_closes(self):
        cb = _cb.CircuitBreaker(name="test", failure_threshold=1, cooldown_seconds=0.01)
        try:
            cb.call(_raise_runtime_error)
        except RuntimeError:
            pass
        _advance(0.02)
        cb.call(lambda: 42)
        assert cb.state == "closed"

    def test_half_open_failure_reopens(self):
        cb = _cb.CircuitBreaker(name="test", failure_threshold=1, cooldown_seconds=0.01)
        try:
            cb.call(_raise_runtime_error)
        except RuntimeError:
            pass
        _advance(0.02)
        try:
            cb.call(_raise_runtime_error)
        except RuntimeError:
            pass
        assert cb.state == "open"
