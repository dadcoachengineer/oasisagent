"""Abstract base class for ingestion adapters.

All ingestion adapters implement this interface. The agent's main loop
manages adapter lifecycle via start() and stop().
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from oasisagent.engine.queue import EventQueue


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
