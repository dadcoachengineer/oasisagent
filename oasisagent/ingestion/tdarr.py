"""Tdarr polling ingestion adapter.

Polls the Tdarr API v2 for worker health and queue status.

Events emitted on state transitions:
- ``tdarr_worker_failed`` (WARNING) when a worker goes offline
- ``tdarr_worker_recovered`` (INFO) when a worker comes back online
- ``tdarr_queue_stuck`` (WARNING) when queue items are not progressing
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import aiohttp

from oasisagent.ingestion.base import IngestAdapter
from oasisagent.models import Event, EventMetadata, Severity

if TYPE_CHECKING:
    from oasisagent.config import TdarrAdapterConfig
    from oasisagent.engine.queue import EventQueue

logger = logging.getLogger(__name__)

# Queue is considered stuck if no items processed in this many polls
_STUCK_POLL_THRESHOLD = 3


class TdarrAdapter(IngestAdapter):
    """Polls Tdarr for worker health and queue status.

    State-based dedup ensures events only fire on transitions.
    """

    def __init__(
        self, config: TdarrAdapterConfig, queue: EventQueue,
    ) -> None:
        super().__init__(queue)
        self._config = config
        self._stopping = False
        self._task: asyncio.Task[None] | None = None
        self._connected = False

        # State trackers
        self._worker_states: dict[str, bool] = {}  # worker_id -> is_online
        self._last_queue_processed: int | None = None
        self._stall_counter: int = 0
        self._queue_stuck: bool = False

    @property
    def name(self) -> str:
        return "tdarr"

    async def start(self) -> None:
        self._task = asyncio.create_task(
            self._poll_loop(), name="tdarr-poller",
        )
        await self._task

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def healthy(self) -> bool:
        return self._connected

    # -----------------------------------------------------------------
    # Poll loop
    # -----------------------------------------------------------------

    async def _poll_loop(self) -> None:
        timeout = aiohttp.ClientTimeout(total=self._config.timeout)

        while not self._stopping:
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    await self._poll_status(session)
                    self._connected = True
            except asyncio.CancelledError:
                return
            except (TimeoutError, aiohttp.ClientError) as exc:
                if self._connected:
                    logger.warning("Tdarr: connection error: %s", exc)
                self._connected = False
            except Exception:
                self._connected = False
                logger.exception("Tdarr: unexpected error")

            for _ in range(self._config.poll_interval):
                if self._stopping:
                    return
                await asyncio.sleep(1)

    # -----------------------------------------------------------------
    # Status endpoint
    # -----------------------------------------------------------------

    async def _poll_status(self, session: aiohttp.ClientSession) -> None:
        """Poll /api/v2/status for worker and queue status."""
        url = f"{self._config.url}/api/v2/status"
        async with session.get(url) as resp:
            resp.raise_for_status()
            data: dict[str, Any] = await resp.json(content_type=None)

        self._check_workers(data)
        self._check_queue(data)

    def _check_workers(self, data: dict[str, Any]) -> None:
        """Check worker status for offline transitions."""
        workers: dict[str, Any] = data.get("workers", {})

        for worker_id, worker_info in workers.items():
            info: dict[str, Any] = worker_info if isinstance(worker_info, dict) else {}
            is_online = bool(info) and info.get("idle", True) is not None
            was_online = self._worker_states.get(worker_id)

            self._worker_states[worker_id] = is_online

            if was_online is not None and was_online and not is_online:
                self._enqueue(Event(
                    source=self.name,
                    system="tdarr",
                    event_type="tdarr_worker_failed",
                    entity_id=worker_id,
                    severity=Severity.WARNING,
                    timestamp=datetime.now(tz=UTC),
                    payload={
                        "worker_id": worker_id,
                        "worker_type": info.get("workerType", ""),
                    },
                    metadata=EventMetadata(
                        dedup_key=f"tdarr:worker:{worker_id}",
                    ),
                ))
            elif was_online is not None and not was_online and is_online:
                self._enqueue(Event(
                    source=self.name,
                    system="tdarr",
                    event_type="tdarr_worker_recovered",
                    entity_id=worker_id,
                    severity=Severity.INFO,
                    timestamp=datetime.now(tz=UTC),
                    payload={"worker_id": worker_id},
                    metadata=EventMetadata(
                        dedup_key=f"tdarr:worker:{worker_id}",
                    ),
                ))
            elif was_online is None and not is_online:
                # First poll, already offline
                self._enqueue(Event(
                    source=self.name,
                    system="tdarr",
                    event_type="tdarr_worker_failed",
                    entity_id=worker_id,
                    severity=Severity.WARNING,
                    timestamp=datetime.now(tz=UTC),
                    payload={
                        "worker_id": worker_id,
                        "worker_type": info.get("workerType", ""),
                    },
                    metadata=EventMetadata(
                        dedup_key=f"tdarr:worker:{worker_id}",
                    ),
                ))

    def _check_queue(self, data: dict[str, Any]) -> None:
        """Check queue progress for stuck detection."""
        queue_count = data.get("queueCount", 0)
        processed_count = data.get("processedCount", 0)

        if queue_count == 0:
            # Nothing queued, reset stall tracking
            if self._queue_stuck:
                self._enqueue(Event(
                    source=self.name,
                    system="tdarr",
                    event_type="tdarr_queue_recovered",
                    entity_id="tdarr",
                    severity=Severity.INFO,
                    timestamp=datetime.now(tz=UTC),
                    payload={"queue_count": 0, "processed_count": processed_count},
                    metadata=EventMetadata(
                        dedup_key="tdarr:queue:stuck",
                    ),
                ))
            self._stall_counter = 0
            self._queue_stuck = False
            self._last_queue_processed = processed_count
            return

        # Queue has items — check if progress is being made
        if self._last_queue_processed is not None:
            if processed_count <= self._last_queue_processed:
                self._stall_counter += 1
            else:
                self._stall_counter = 0
                if self._queue_stuck:
                    self._queue_stuck = False
                    self._enqueue(Event(
                        source=self.name,
                        system="tdarr",
                        event_type="tdarr_queue_recovered",
                        entity_id="tdarr",
                        severity=Severity.INFO,
                        timestamp=datetime.now(tz=UTC),
                        payload={
                            "queue_count": queue_count,
                            "processed_count": processed_count,
                        },
                        metadata=EventMetadata(
                            dedup_key="tdarr:queue:stuck",
                        ),
                    ))

        self._last_queue_processed = processed_count

        if self._stall_counter >= _STUCK_POLL_THRESHOLD and not self._queue_stuck:
            self._queue_stuck = True
            self._enqueue(Event(
                source=self.name,
                system="tdarr",
                event_type="tdarr_queue_stuck",
                entity_id="tdarr",
                severity=Severity.WARNING,
                timestamp=datetime.now(tz=UTC),
                payload={
                    "queue_count": queue_count,
                    "processed_count": processed_count,
                    "stalled_polls": self._stall_counter,
                },
                metadata=EventMetadata(
                    dedup_key="tdarr:queue:stuck",
                ),
            ))

    # -----------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------

    def _enqueue(self, event: Event) -> None:
        """Enqueue an event, logging on failure."""
        try:
            self._queue.put_nowait(event)
        except Exception:
            logger.warning(
                "Tdarr: failed to enqueue event: %s/%s",
                event.system, event.event_type,
            )
