"""Abstract base class for ingestion adapters.

All ingestion adapters implement this interface. The agent's main loop
manages adapter lifecycle via start() and stop().
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from oasisagent.engine.queue import EventQueue
    from oasisagent.models import TopologyEdge, TopologyNode
    from oasisagent.models.event import Event

logger = logging.getLogger(__name__)


class IngestAdapter(ABC):
    """Base class for all ingestion adapters.

    Adapters connect to external event sources, transform raw data into
    canonical Event objects, and push them to the EventQueue.

    Args:
        queue: The shared event queue to push events into.
    """

    def __init__(self, queue: EventQueue) -> None:
        self._queue = queue

    @property
    @abstractmethod
    def name(self) -> str:
        """Adapter identifier, used in Event.source field."""

    @abstractmethod
    async def start(self) -> None:
        """Start listening/polling. Runs as a long-lived async task.

        Implementations should handle reconnection internally and never
        let exceptions propagate — log and retry on transient failures,
        log and stop on fatal configuration errors.
        """

    @abstractmethod
    async def stop(self) -> None:
        """Graceful shutdown. Cancel loops, close connections."""

    @abstractmethod
    async def healthy(self) -> bool:
        """Return whether the adapter currently has an active connection.

        Used by the agent for health checks and status reporting.
        """

    async def discover_topology(
        self,
    ) -> tuple[list[TopologyNode], list[TopologyEdge]]:
        """Discover services and relationships this adapter knows about.

        Returns (nodes, edges) for the service topology graph. Default
        implementation returns empty lists — adapters override this to
        provide auto-discovery.

        Called periodically by the orchestrator to keep the topology
        graph up-to-date.
        """
        return [], []

    def _enqueue(self, event: Event) -> None:
        """Enqueue an event, logging on failure."""
        try:
            self._queue.put_nowait(event)
        except Exception:
            logger.warning(
                "%s: failed to enqueue event: %s/%s",
                self.name,
                event.system,
                event.event_type,
            )
