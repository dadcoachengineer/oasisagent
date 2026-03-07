"""Event queue with deduplication and backpressure.

This module lives in ``engine/`` because the queue is part of the processing
pipeline: ingestion adapters push events in, the decision engine consumes them.
ARCHITECTURE.md §2 shows the Event Queue sitting between Ingestion Adapters and
the Decision Engine — it's infrastructure for the engine, not a standalone
component.

Backpressure: when the queue is full, ``put()`` blocks the calling adapter,
naturally slowing ingestion. ``put_nowait()`` raises ``QueueFullError`` for
adapters that can't afford to block.

Deduplication: events with the same ``metadata.dedup_key`` within a
configurable window are silently dropped. This is enforced at the queue
level so individual adapters don't need to coordinate.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from oasisagent.models import Event

logger = logging.getLogger(__name__)


class QueueFullError(Exception):
    """Raised when ``put_nowait()`` is called on a full queue."""


class EventQueue:
    """Async event queue with deduplication and backpressure.

    Args:
        max_size: Maximum number of events in the queue. When full,
            ``put()`` blocks and ``put_nowait()`` raises ``QueueFullError``.
        dedup_window_seconds: Events with the same ``metadata.dedup_key``
            within this window are silently dropped. Set to 0 to disable.
    """

    # Warn once when queue crosses this threshold; reset when it drops below
    # the recovery threshold. Classic hysteresis to avoid log spam.
    _WARN_THRESHOLD = 0.8
    _RECOVER_THRESHOLD = 0.5
    _PRUNE_INTERVAL_SECONDS = 60.0

    def __init__(self, max_size: int = 1000, dedup_window_seconds: int = 300) -> None:
        self._queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=max_size)
        self._max_size = max_size
        self._dedup_window = dedup_window_seconds
        self._seen: dict[str, float] = {}  # dedup_key -> timestamp
        self._last_prune: float = time.monotonic()
        self._capacity_warning_active = False

    # -- Public interface ---------------------------------------------------

    async def put(self, event: Event) -> bool:
        """Enqueue an event. Blocks if the queue is full (backpressure).

        Returns:
            True if the event was enqueued, False if it was deduplicated.
        """
        if self._is_duplicate(event):
            logger.debug("Dedup: dropping event %s (key=%s)", event.id, event.metadata.dedup_key)
            return False

        await self._queue.put(event)
        self._record_seen(event)
        self._check_capacity()
        return True

    def put_nowait(self, event: Event) -> bool:
        """Enqueue an event without blocking.

        Returns:
            True if the event was enqueued, False if it was deduplicated.

        Raises:
            QueueFullError: If the queue is at capacity.
        """
        if self._is_duplicate(event):
            logger.debug("Dedup: dropping event %s (key=%s)", event.id, event.metadata.dedup_key)
            return False

        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull as exc:
            raise QueueFullError(
                f"Event queue is full ({self._max_size} events). "
                f"The decision engine may be falling behind."
            ) from exc

        self._record_seen(event)
        self._check_capacity()
        return True

    async def get(self) -> Event:
        """Dequeue the next event. Blocks until one is available."""
        event = await self._queue.get()
        self._check_capacity_recovery()
        return event

    def task_done(self) -> None:
        """Mark a dequeued event as fully processed."""
        self._queue.task_done()

    def drain(self) -> list[Event]:
        """Remove and return all queued events without blocking.

        Call after all producers have stopped. Not safe for concurrent
        use with ``put()``.
        """
        events: list[Event] = []
        while not self._queue.empty():
            try:
                events.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        return events

    @property
    def size(self) -> int:
        """Number of events currently in the queue."""
        return self._queue.qsize()

    @property
    def is_full(self) -> bool:
        """Whether the queue is at capacity."""
        return self._queue.full()

    @property
    def is_empty(self) -> bool:
        """Whether the queue has no events."""
        return self._queue.empty()

    @property
    def dedup_cache_size(self) -> int:
        """Number of entries in the dedup cache (for monitoring/testing)."""
        return len(self._seen)

    # -- Deduplication ------------------------------------------------------

    def _is_duplicate(self, event: Event) -> bool:
        """Check if this event's dedup_key was seen within the window."""
        if self._dedup_window <= 0:
            return False

        key = event.metadata.dedup_key
        if not key:
            return False

        self._maybe_prune()

        seen_at = self._seen.get(key)
        if seen_at is None:
            return False

        return (time.monotonic() - seen_at) < self._dedup_window

    def _record_seen(self, event: Event) -> None:
        """Record a dedup key with the current timestamp."""
        if self._dedup_window <= 0:
            return

        key = event.metadata.dedup_key
        if key:
            self._seen[key] = time.monotonic()

    def _maybe_prune(self) -> None:
        """Prune expired dedup entries if enough time has passed.

        Uses a time gate to avoid pruning on every put(). Only prunes
        if >60s since last prune.
        """
        now = time.monotonic()
        if (now - self._last_prune) < self._PRUNE_INTERVAL_SECONDS:
            return

        self._last_prune = now
        cutoff = now - self._dedup_window
        expired = [k for k, ts in self._seen.items() if ts < cutoff]
        for k in expired:
            del self._seen[k]

        if expired:
            logger.debug("Dedup: pruned %d expired entries", len(expired))

    # -- Capacity monitoring ------------------------------------------------

    def _check_capacity(self) -> None:
        """Warn once when queue crosses the high-water mark."""
        if self._capacity_warning_active:
            return

        fill_ratio = self._queue.qsize() / self._max_size
        if fill_ratio >= self._WARN_THRESHOLD:
            self._capacity_warning_active = True
            logger.warning(
                "Event queue at %.0f%% capacity (%d/%d). "
                "Decision engine may be falling behind.",
                fill_ratio * 100,
                self._queue.qsize(),
                self._max_size,
            )

    def _check_capacity_recovery(self) -> None:
        """Reset the capacity warning when the queue drops below recovery threshold."""
        if not self._capacity_warning_active:
            return

        fill_ratio = self._queue.qsize() / self._max_size
        if fill_ratio <= self._RECOVER_THRESHOLD:
            self._capacity_warning_active = False
            logger.info(
                "Event queue recovered to %.0f%% capacity (%d/%d).",
                fill_ratio * 100,
                self._queue.qsize(),
                self._max_size,
            )
