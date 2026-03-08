"""Circuit breaker — prevents remediation loops.

Tracks action attempts per entity and globally. Trips when:
- Per-entity: max attempts exceeded in rolling window, or cooldown active
- Global: failure rate exceeds threshold (with minimum sample size)

When tripped:
- Entity trip: AUTO_FIX → RECOMMEND (notify human instead)
- Entity cooldown: defer action (too soon since last attempt)
- Global trip: AUTO_FIX → ESCALATE (global pause)

ARCHITECTURE.md §7 describes the circuit breaker behavior.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, NamedTuple

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from oasisagent.config import CircuitBreakerConfig

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal bookkeeping
# ---------------------------------------------------------------------------


class AttemptRecord(NamedTuple):
    """A single recorded action attempt."""

    entity_id: str
    timestamp: float  # time.monotonic()
    success: bool


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------


class CircuitBreakerResult(BaseModel):
    """Outcome of a circuit breaker check."""

    model_config = ConfigDict(extra="forbid")

    allowed: bool
    reason: str
    entity_tripped: bool = False
    entity_cooldown: bool = False
    global_tripped: bool = False


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------


class CircuitBreaker:
    """Tracks action outcomes and prevents remediation loops.

    Maintains a single list of attempt records. Per-entity and global
    state are both derived from this list, ensuring a single source
    of truth.

    Uses ``time.monotonic()`` for timestamps — immune to clock skew
    and NTP jumps.
    """

    def __init__(self, config: CircuitBreakerConfig) -> None:
        self._config = config
        self._attempts: list[AttemptRecord] = []
        self._global_tripped_at: float | None = None

    def record_attempt(
        self, entity_id: str, *, success: bool
    ) -> CircuitBreakerResult:
        """Record an action outcome and return the current circuit state.

        The caller gets immediate feedback on whether this attempt
        tripped the circuit, without needing a separate check() call.
        """
        self._attempts.append(AttemptRecord(
            entity_id=entity_id,
            timestamp=time.monotonic(),
            success=success,
        ))
        return self.check(entity_id)

    def check(self, entity_id: str) -> CircuitBreakerResult:
        """Check if the circuit breaker allows an action for this entity.

        Evaluates in order:
        1. Global trip (failure rate above threshold)
        2. Per-entity max attempts exceeded
        3. Per-entity cooldown active
        """
        self._prune()

        # 1. Global trip check
        if self._is_global_tripped():
            return CircuitBreakerResult(
                allowed=False,
                reason="Global circuit breaker tripped — failure rate exceeded threshold",
                global_tripped=True,
            )

        # 2. Per-entity max attempts
        entity_attempts = [a for a in self._attempts if a.entity_id == entity_id]
        if len(entity_attempts) >= self._config.max_attempts_per_entity:
            logger.info(
                "Circuit breaker: entity %s has %d attempts (max %d) in window",
                entity_id,
                len(entity_attempts),
                self._config.max_attempts_per_entity,
            )
            return CircuitBreakerResult(
                allowed=False,
                reason=(
                    f"Entity '{entity_id}' has {len(entity_attempts)} attempts "
                    f"in the last {self._config.window_minutes} minutes "
                    f"(max {self._config.max_attempts_per_entity})"
                ),
                entity_tripped=True,
            )

        # 3. Per-entity cooldown
        if entity_attempts:
            last_attempt = entity_attempts[-1]
            cooldown_seconds = self._config.cooldown_minutes * 60
            elapsed = time.monotonic() - last_attempt.timestamp
            if elapsed < cooldown_seconds:
                remaining = cooldown_seconds - elapsed
                logger.debug(
                    "Circuit breaker: entity %s in cooldown (%.0fs remaining)",
                    entity_id,
                    remaining,
                )
                return CircuitBreakerResult(
                    allowed=False,
                    reason=(
                        f"Entity '{entity_id}' is in cooldown "
                        f"({remaining:.0f}s remaining of {self._config.cooldown_minutes}m)"
                    ),
                    entity_cooldown=True,
                )

        # All checks passed
        return CircuitBreakerResult(
            allowed=True,
            reason="Circuit breaker allows action",
        )

    def reset(self, entity_id: str | None = None) -> None:
        """Reset circuit breaker state.

        If entity_id is given, only that entity's attempts are cleared.
        If None, all state is reset (including global trip).
        """
        if entity_id is None:
            self._attempts.clear()
            self._global_tripped_at = None
            logger.info("Circuit breaker: full reset")
        else:
            self._attempts = [a for a in self._attempts if a.entity_id != entity_id]
            logger.info("Circuit breaker: reset for entity %s", entity_id)

    def _is_global_tripped(self) -> bool:
        """Check if the global circuit breaker is active."""
        now = time.monotonic()

        # If currently in global pause, check if it has expired
        if self._global_tripped_at is not None:
            pause_seconds = self._config.global_pause_minutes * 60
            if (now - self._global_tripped_at) < pause_seconds:
                return True
            # Pause expired — reset
            self._global_tripped_at = None
            logger.info("Circuit breaker: global pause expired, resuming")

        # Check failure rate (with minimum sample size)
        total = len(self._attempts)
        if total < self._min_global_attempts:
            return False

        failures = sum(1 for a in self._attempts if not a.success)
        rate = failures / total

        if rate >= self._config.global_failure_rate_threshold:
            self._global_tripped_at = now
            logger.warning(
                "Circuit breaker: GLOBAL TRIP — failure rate %.1f%% "
                "(%d/%d) exceeds threshold %.1f%%",
                rate * 100,
                failures,
                total,
                self._config.global_failure_rate_threshold * 100,
            )
            return True

        return False

    @property
    def _min_global_attempts(self) -> int:
        """Minimum attempts before global failure rate is evaluated.

        Prevents false trips on cold start (e.g., 1 failure out of
        1 attempt = 100% rate, but shouldn't trip globally).
        """
        return 5

    def _prune(self) -> None:
        """Remove attempts outside the rolling window."""
        cutoff = time.monotonic() - (self._config.window_minutes * 60)
        self._attempts = [a for a in self._attempts if a.timestamp >= cutoff]
