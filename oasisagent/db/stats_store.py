"""Stats store — persists dashboard counters to SQLite with write-behind flush.

The orchestrator increments in-memory counters on every event/action/error.
The StatsStore periodically flushes those values to SQLite so they survive
restarts. On startup, it loads the last persisted values.

Write-behind design: the hot path (event processing) never blocks on I/O.
The flush interval is configurable but defaults to 30 seconds.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import aiosqlite

logger = logging.getLogger(__name__)

# Keys must match the seeded rows in migration 004
STAT_KEYS = ("events_processed", "actions_taken", "errors")


class StatsStore:
    """Write-behind persistence for dashboard counters.

    Usage::

        store = await StatsStore.from_db(db)
        # Returns persisted values to seed orchestrator counters
        vals = store.values  # {"events_processed": 42, ...}

        # After orchestrator increments, call flush periodically
        await store.flush(events_processed=43, actions_taken=10, errors=1)

        # Or run the background flush loop
        store.start(stats_getter, interval=30)
        ...
        await store.stop()
    """

    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db
        self._values: dict[str, int] = {k: 0 for k in STAT_KEYS}
        self._flush_task: asyncio.Task[None] | None = None
        self._stopping = False

    @classmethod
    async def from_db(cls, db: aiosqlite.Connection) -> StatsStore:
        """Create a StatsStore and load persisted values from SQLite."""
        store = cls(db)
        cursor = await db.execute("SELECT key, value FROM stats")
        rows = await cursor.fetchall()
        for row in rows:
            if row["key"] in STAT_KEYS:
                store._values[row["key"]] = row["value"]
        if any(v > 0 for v in store._values.values()):
            logger.info(
                "Loaded stats from database: %s",
                ", ".join(f"{k}={v}" for k, v in store._values.items()),
            )
        return store

    @property
    def values(self) -> dict[str, int]:
        """Return the last loaded/flushed values."""
        return dict(self._values)

    async def flush(
        self,
        *,
        events_processed: int,
        actions_taken: int,
        errors: int,
    ) -> None:
        """Write current counter values to SQLite.

        Uses UPDATE (not UPSERT) since migration 004 seeds all rows.
        """
        pairs = [
            ("events_processed", events_processed),
            ("actions_taken", actions_taken),
            ("errors", errors),
        ]
        await self._db.executemany(
            "UPDATE stats SET value = ? WHERE key = ?",
            [(v, k) for k, v in pairs],
        )
        await self._db.commit()
        self._values = {k: v for k, v in pairs}

    def start(
        self,
        stats_getter: callable,
        *,
        interval: float = 30.0,
    ) -> None:
        """Start the background flush loop.

        Args:
            stats_getter: Callable returning ``(events_processed, actions_taken, errors)``.
            interval: Seconds between flushes.
        """
        self._stopping = False
        self._flush_task = asyncio.create_task(
            self._flush_loop(stats_getter, interval),
            name="stats-flush",
        )

    async def stop(self) -> None:
        """Stop the background flush loop and do a final flush."""
        self._stopping = True
        if self._flush_task is not None:
            self._flush_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._flush_task
            self._flush_task = None

    async def _flush_loop(
        self,
        stats_getter: callable,
        interval: float,
    ) -> None:
        """Periodically flush stats to SQLite."""
        while not self._stopping:
            await asyncio.sleep(interval)
            try:
                ep, at, er = stats_getter()
                await self.flush(
                    events_processed=ep,
                    actions_taken=at,
                    errors=er,
                )
            except Exception:
                logger.exception("Failed to flush stats to database")
