"""Exponential backoff utility for reconnection logic.

Used by all ingestion adapters (and potentially handlers) that need
retry-with-backoff on transient connection failures.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


class ExponentialBackoff:
    """Tracks exponential backoff state for reconnection loops.

    Args:
        initial: Initial delay in seconds.
        maximum: Maximum delay cap in seconds.
        multiplier: Delay multiplier after each attempt.
        name: Identifier for log messages.
    """

    def __init__(
        self,
        initial: float = 1.0,
        maximum: float = 60.0,
        multiplier: float = 2.0,
        name: str = "",
    ) -> None:
        self._initial = initial
        self._maximum = maximum
        self._multiplier = multiplier
        self._name = name
        self._current = initial
        self._attempts = 0

    async def wait(self) -> None:
        """Sleep for the current backoff duration, then increase it."""
        self._attempts += 1
        delay = min(self._current, self._maximum)
        logger.info(
            "%s: reconnecting in %.1fs (attempt %d)",
            self._name or "backoff",
            delay,
            self._attempts,
        )
        await asyncio.sleep(delay)
        self._current = min(self._current * self._multiplier, self._maximum)

    def reset(self) -> None:
        """Reset backoff to initial delay after a successful connection."""
        self._current = self._initial
        self._attempts = 0

    @property
    def attempts(self) -> int:
        """Number of backoff attempts since last reset."""
        return self._attempts
