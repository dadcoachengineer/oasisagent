"""Base class for scanner ingestion adapters.

Scanners extend IngestAdapter with a fixed-interval poll loop. Each concrete
scanner implements ``_scan()`` to run one check cycle and return events to emit.

The poll loop sleeps in 1-second increments for responsive shutdown, matching
the pattern used by ``HttpPollerAdapter``.
"""

from __future__ import annotations

import asyncio
import logging
from abc import abstractmethod
from typing import TYPE_CHECKING

from oasisagent.ingestion.base import IngestAdapter

if TYPE_CHECKING:
    from oasisagent.engine.queue import EventQueue
    from oasisagent.models import Event

logger = logging.getLogger(__name__)


class ScannerIngestAdapter(IngestAdapter):
    """Base class for scanner adapters. Provides poll loop and enqueue helper.

    Subclasses must implement ``_scan()`` and the ``name`` property.
    """

    def __init__(
        self,
        queue: EventQueue,
        interval: int,
        *,
        adaptive_enabled: bool = True,
        adaptive_fast_factor: float = 0.25,
        adaptive_recovery_scans: int = 3,
    ) -> None:
        super().__init__(queue)
        self._interval = interval
        self._stopping = False
        self._task: asyncio.Task[None] | None = None
        self._connected = True

        # Adaptive interval state
        self._adaptive_enabled = adaptive_enabled
        self._adaptive_fast_factor = adaptive_fast_factor
        self._adaptive_recovery_scans = adaptive_recovery_scans
        self._consecutive_clean: int = 0
        self._adapted: bool = False

    @property
    def _effective_interval(self) -> int:
        """Return the current polling interval, shortened when in fast mode."""
        if self._adaptive_enabled and self._adapted:
            return max(60, int(self._interval * self._adaptive_fast_factor))
        return self._interval

    @abstractmethod
    async def _scan(self) -> list[Event]:
        """Run one scan cycle. Return events to emit."""

    async def start(self) -> None:
        """Start the polling loop. Blocks until stop() is called or cancelled."""
        self._task = asyncio.create_task(
            self._poll_loop(), name=f"scanner-{self.name}",
        )
        await self._task

    async def stop(self) -> None:
        """Signal the polling loop to stop and cancel the task."""
        self._stopping = True
        if self._task:
            self._task.cancel()
            self._task = None

    async def healthy(self) -> bool:
        """Return whether the last scan completed without error."""
        return self._connected

    async def _poll_loop(self) -> None:
        """Poll at intervals, emitting events from each scan cycle.

        When adaptive intervals are enabled, the scanner switches to a faster
        polling rate after detecting WARNING+ events, then restores the normal
        interval after consecutive clean scans.
        """
        while not self._stopping:
            try:
                events = await self._scan()
                for event in events:
                    self._enqueue(event)
                self._connected = True
                self._update_adaptive_state(events)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.warning("Scanner %s error: %s", self.name, exc)
                self._connected = False

            # 1-second sleep increments for responsive shutdown
            for _ in range(self._effective_interval):
                if self._stopping:
                    return
                await asyncio.sleep(1)

    def _update_adaptive_state(self, events: list[Event]) -> None:
        """Update adaptive interval state based on scan results."""
        if not self._adaptive_enabled:
            return

        from oasisagent.models import Severity

        has_issues = any(
            e.severity in (Severity.WARNING, Severity.ERROR, Severity.CRITICAL)
            for e in events
        )

        if has_issues:
            if not self._adapted:
                old_interval = self._interval
                self._adapted = True
                logger.info(
                    "Scanner %s: entering fast mode (%ds -> %ds)",
                    self.name, old_interval, self._effective_interval,
                )
            self._consecutive_clean = 0
        elif self._adapted:
            self._consecutive_clean += 1
            if self._consecutive_clean >= self._adaptive_recovery_scans:
                logger.info(
                    "Scanner %s: exiting fast mode (%ds -> %ds)",
                    self.name, self._effective_interval, self._interval,
                )
                self._adapted = False
                self._consecutive_clean = 0

    def _enqueue(self, event: Event) -> None:
        """Enqueue an event, logging on failure."""
        try:
            self._queue.put_nowait(event)
        except Exception:
            logger.warning(
                "Scanner %s: failed to enqueue event: %s/%s",
                self.name, event.system, event.event_type,
            )
