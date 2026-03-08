"""Tests for circuit breaker logic."""

from __future__ import annotations

import time
from typing import Any

from oasisagent.config import CircuitBreakerConfig
from oasisagent.engine.circuit_breaker import CircuitBreaker

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(**overrides: Any) -> CircuitBreakerConfig:
    defaults: dict[str, Any] = {
        "max_attempts_per_entity": 3,
        "window_minutes": 60,
        "cooldown_minutes": 15,
        "global_failure_rate_threshold": 0.3,
        "global_pause_minutes": 30,
    }
    defaults.update(overrides)
    return CircuitBreakerConfig(**defaults)


def _breaker(**overrides: Any) -> CircuitBreaker:
    return CircuitBreaker(_make_config(**overrides))


def _age_attempts(breaker: CircuitBreaker, seconds: float) -> None:
    """Shift all attempt timestamps back by the given seconds."""
    breaker._attempts = [
        a._replace(timestamp=a.timestamp - seconds)
        for a in breaker._attempts
    ]


# ---------------------------------------------------------------------------
# Per-entity: max attempts
# ---------------------------------------------------------------------------


class TestEntityMaxAttempts:
    """Per-entity circuit trips when max attempts exceeded in window."""

    def test_below_threshold_allowed(self) -> None:
        cb = _breaker(max_attempts_per_entity=3)
        cb.record_attempt("light.kitchen", success=False)
        cb.record_attempt("light.kitchen", success=False)

        result = cb.check("light.kitchen")
        # 2 attempts < 3 max, but cooldown may be active
        # Check that entity_tripped is False
        assert result.entity_tripped is False

    def test_at_threshold_tripped(self) -> None:
        cb = _breaker(max_attempts_per_entity=3, cooldown_minutes=0)
        cb.record_attempt("light.kitchen", success=False)
        cb.record_attempt("light.kitchen", success=False)
        cb.record_attempt("light.kitchen", success=False)

        result = cb.check("light.kitchen")
        assert result.allowed is False
        assert result.entity_tripped is True
        assert result.entity_cooldown is False
        assert "3 attempts" in result.reason

    def test_success_counts_toward_max(self) -> None:
        """All attempts count, not just failures."""
        cb = _breaker(max_attempts_per_entity=3, cooldown_minutes=0)
        cb.record_attempt("light.kitchen", success=True)
        cb.record_attempt("light.kitchen", success=True)
        cb.record_attempt("light.kitchen", success=True)

        result = cb.check("light.kitchen")
        assert result.entity_tripped is True

    def test_expired_attempts_dont_count(self) -> None:
        cb = _breaker(max_attempts_per_entity=3, cooldown_minutes=0, window_minutes=60)
        cb.record_attempt("light.kitchen", success=False)
        cb.record_attempt("light.kitchen", success=False)
        cb.record_attempt("light.kitchen", success=False)

        # Age attempts past the window
        _age_attempts(cb, 61 * 60)

        result = cb.check("light.kitchen")
        assert result.allowed is True
        assert result.entity_tripped is False


# ---------------------------------------------------------------------------
# Per-entity: cooldown
# ---------------------------------------------------------------------------


class TestEntityCooldown:
    """Per-entity cooldown blocks attempts too soon after the last one."""

    def test_within_cooldown_blocked(self) -> None:
        cb = _breaker(cooldown_minutes=15, max_attempts_per_entity=10)
        cb.record_attempt("light.kitchen", success=False)

        result = cb.check("light.kitchen")
        assert result.allowed is False
        assert result.entity_cooldown is True
        assert result.entity_tripped is False
        assert "cooldown" in result.reason.lower()

    def test_after_cooldown_allowed(self) -> None:
        cb = _breaker(cooldown_minutes=15, max_attempts_per_entity=10)
        cb.record_attempt("light.kitchen", success=False)

        # Age past cooldown
        _age_attempts(cb, 16 * 60)

        result = cb.check("light.kitchen")
        assert result.allowed is True

    def test_zero_cooldown_allows_immediately(self) -> None:
        cb = _breaker(cooldown_minutes=0, max_attempts_per_entity=10)
        cb.record_attempt("light.kitchen", success=False)

        # With 0 cooldown, the elapsed time (however small) >= 0
        # But monotonic time is > 0 so this should pass
        result = cb.check("light.kitchen")
        assert result.entity_cooldown is False


# ---------------------------------------------------------------------------
# Global trip
# ---------------------------------------------------------------------------


class TestGlobalTrip:
    """Global circuit trips when failure rate exceeds threshold."""

    def test_high_failure_rate_trips(self) -> None:
        cb = _breaker(
            global_failure_rate_threshold=0.3,
            max_attempts_per_entity=100,
            cooldown_minutes=0,
        )
        # 4 failures out of 6 = 66% > 30%
        for i in range(4):
            cb.record_attempt(f"entity.{i}", success=False)
        for i in range(2):
            cb.record_attempt(f"entity.ok_{i}", success=True)

        result = cb.check("entity.new")
        assert result.allowed is False
        assert result.global_tripped is True

    def test_low_failure_rate_allowed(self) -> None:
        cb = _breaker(
            global_failure_rate_threshold=0.3,
            max_attempts_per_entity=100,
            cooldown_minutes=0,
        )
        # 1 failure out of 6 = 16% < 30%
        cb.record_attempt("entity.bad", success=False)
        for i in range(5):
            cb.record_attempt(f"entity.ok_{i}", success=True)

        result = cb.check("entity.new")
        assert result.global_tripped is False

    def test_below_min_sample_size_not_tripped(self) -> None:
        """1 failure out of 1 attempt = 100%, but below min sample (5)."""
        cb = _breaker(global_failure_rate_threshold=0.3, cooldown_minutes=0)
        cb.record_attempt("entity.bad", success=False)

        result = cb.check("entity.other")
        assert result.global_tripped is False
        assert result.allowed is True

    def test_exactly_at_min_sample_size(self) -> None:
        """5 attempts, all failures = 100% → trips."""
        cb = _breaker(
            global_failure_rate_threshold=0.3,
            max_attempts_per_entity=100,
            cooldown_minutes=0,
        )
        for i in range(5):
            cb.record_attempt(f"entity.bad_{i}", success=False)

        result = cb.check("entity.new")
        assert result.global_tripped is True

    def test_global_pause_expires(self) -> None:
        cb = _breaker(
            global_failure_rate_threshold=0.3,
            global_pause_minutes=30,
            max_attempts_per_entity=100,
            cooldown_minutes=0,
        )
        # Trip the global breaker
        for i in range(5):
            cb.record_attempt(f"entity.bad_{i}", success=False)

        result = cb.check("entity.new")
        assert result.global_tripped is True

        # Age attempts past the window AND simulate pause expiry
        _age_attempts(cb, 61 * 60)
        cb._global_tripped_at = time.monotonic() - (31 * 60)

        result = cb.check("entity.new")
        assert result.global_tripped is False
        assert result.allowed is True

    def test_global_pause_still_active(self) -> None:
        cb = _breaker(
            global_failure_rate_threshold=0.3,
            global_pause_minutes=30,
            max_attempts_per_entity=100,
            cooldown_minutes=0,
        )
        for i in range(5):
            cb.record_attempt(f"entity.bad_{i}", success=False)

        # Trip it
        cb.check("entity.new")

        # Only 10 minutes have passed (pause is 30)
        cb._global_tripped_at = time.monotonic() - (10 * 60)

        result = cb.check("entity.new")
        assert result.global_tripped is True
        assert result.allowed is False


# ---------------------------------------------------------------------------
# Entity isolation
# ---------------------------------------------------------------------------


class TestEntityIsolation:
    """Failures for one entity don't affect another entity's per-entity check."""

    def test_entity_a_tripped_entity_b_allowed(self) -> None:
        cb = _breaker(max_attempts_per_entity=3, cooldown_minutes=0)
        cb.record_attempt("entity.a", success=False)
        cb.record_attempt("entity.a", success=False)
        cb.record_attempt("entity.a", success=False)

        result_a = cb.check("entity.a")
        result_b = cb.check("entity.b")

        assert result_a.entity_tripped is True
        assert result_b.allowed is True
        assert result_b.entity_tripped is False

    def test_independent_cooldowns(self) -> None:
        cb = _breaker(cooldown_minutes=15, max_attempts_per_entity=10)
        cb.record_attempt("entity.a", success=False)

        result_a = cb.check("entity.a")
        result_b = cb.check("entity.b")

        assert result_a.entity_cooldown is True
        assert result_b.allowed is True


# ---------------------------------------------------------------------------
# Combined states
# ---------------------------------------------------------------------------


class TestCombinedStates:
    """Entity tripped + global ok, entity ok + global tripped."""

    def test_entity_tripped_global_ok(self) -> None:
        cb = _breaker(
            max_attempts_per_entity=2,
            cooldown_minutes=0,
            global_failure_rate_threshold=0.9,
        )
        cb.record_attempt("entity.a", success=False)
        cb.record_attempt("entity.a", success=False)
        # Add successes to keep global rate low
        for i in range(10):
            cb.record_attempt(f"entity.ok_{i}", success=True)

        result = cb.check("entity.a")
        assert result.entity_tripped is True
        assert result.global_tripped is False

    def test_entity_ok_global_tripped(self) -> None:
        cb = _breaker(
            max_attempts_per_entity=100,
            cooldown_minutes=0,
            global_failure_rate_threshold=0.3,
        )
        for i in range(5):
            cb.record_attempt(f"entity.bad_{i}", success=False)

        # entity.clean has no attempts, but global is tripped
        result = cb.check("entity.clean")
        assert result.global_tripped is True
        assert result.entity_tripped is False


# ---------------------------------------------------------------------------
# record_attempt returns result
# ---------------------------------------------------------------------------


class TestRecordAttempt:
    """record_attempt returns CircuitBreakerResult for immediate feedback."""

    def test_returns_result(self) -> None:
        cb = _breaker(max_attempts_per_entity=2, cooldown_minutes=0)
        result = cb.record_attempt("light.kitchen", success=False)

        assert result.allowed is True  # 1 attempt < 2 max

    def test_tripping_attempt_returns_tripped(self) -> None:
        cb = _breaker(max_attempts_per_entity=2, cooldown_minutes=0)
        cb.record_attempt("light.kitchen", success=False)
        result = cb.record_attempt("light.kitchen", success=False)

        assert result.entity_tripped is True
        assert result.allowed is False


# ---------------------------------------------------------------------------
# Reset
# ---------------------------------------------------------------------------


class TestReset:
    """Reset clears circuit breaker state."""

    def test_reset_entity(self) -> None:
        cb = _breaker(max_attempts_per_entity=2, cooldown_minutes=0)
        cb.record_attempt("entity.a", success=False)
        cb.record_attempt("entity.a", success=False)

        assert cb.check("entity.a").entity_tripped is True

        cb.reset("entity.a")

        assert cb.check("entity.a").allowed is True

    def test_reset_entity_preserves_others(self) -> None:
        cb = _breaker(max_attempts_per_entity=2, cooldown_minutes=0)
        cb.record_attempt("entity.a", success=False)
        cb.record_attempt("entity.a", success=False)
        cb.record_attempt("entity.b", success=False)
        cb.record_attempt("entity.b", success=False)

        cb.reset("entity.a")

        assert cb.check("entity.a").allowed is True
        assert cb.check("entity.b").entity_tripped is True

    def test_reset_all(self) -> None:
        cb = _breaker(
            max_attempts_per_entity=2,
            cooldown_minutes=0,
            global_failure_rate_threshold=0.3,
        )
        for i in range(5):
            cb.record_attempt(f"entity.bad_{i}", success=False)

        assert cb.check("entity.new").global_tripped is True

        cb.reset()

        assert cb.check("entity.new").allowed is True


# ---------------------------------------------------------------------------
# Empty state
# ---------------------------------------------------------------------------


class TestEmptyState:
    """No attempts recorded — everything allowed."""

    def test_no_attempts_allowed(self) -> None:
        cb = _breaker()
        result = cb.check("any.entity")

        assert result.allowed is True
        assert result.entity_tripped is False
        assert result.entity_cooldown is False
        assert result.global_tripped is False
