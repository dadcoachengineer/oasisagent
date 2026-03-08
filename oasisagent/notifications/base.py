"""Notification channel abstract base class.

All notification channels implement this interface. The dispatcher
fans out notifications to all enabled channels.

ARCHITECTURE.md §10 defines the notification contract.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from oasisagent.models import Notification


class NotificationChannel(ABC):
    """Base class for notification channels (MQTT, email, webhook, etc.)."""

    @abstractmethod
    async def send(self, notification: Notification) -> bool:
        """Send a notification. Returns True on success."""

    @abstractmethod
    def name(self) -> str:
        """Channel identifier for logging and result tracking."""

    async def start(self) -> None:  # noqa: B027 — intentional non-abstract default
        """Initialize channel resources. Default no-op."""

    async def stop(self) -> None:  # noqa: B027 — intentional non-abstract default
        """Clean up channel resources. Default no-op."""
