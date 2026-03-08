"""Notification dispatcher — fans out notifications to all enabled channels.

The dispatcher owns the lifecycle of all channels and provides a single
point of entry for sending notifications.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from oasisagent.models import Notification
    from oasisagent.notifications.base import NotificationChannel

logger = logging.getLogger(__name__)


class NotificationDispatcher:
    """Sends notifications to all registered channels.

    One channel failing (connect, send, or disconnect) does not
    affect the others.
    """

    def __init__(self, channels: list[NotificationChannel]) -> None:
        self._channels = channels

    async def start(self) -> None:
        """Start all channels. Failures are logged, not raised."""
        for channel in self._channels:
            try:
                await channel.start()
            except Exception as exc:
                logger.error(
                    "Notification channel '%s' failed to start: %s",
                    channel.name(),
                    exc,
                )

    async def stop(self) -> None:
        """Stop all channels. Failures are logged, not raised."""
        for channel in self._channels:
            try:
                await channel.stop()
            except Exception as exc:
                logger.error(
                    "Notification channel '%s' failed to stop: %s",
                    channel.name(),
                    exc,
                )

    async def dispatch(self, notification: Notification) -> dict[str, bool]:
        """Send a notification to all channels.

        Returns a dict mapping channel name → success boolean.
        """
        results: dict[str, bool] = {}
        for channel in self._channels:
            try:
                results[channel.name()] = await channel.send(notification)
            except Exception as exc:
                logger.warning(
                    "Notification channel '%s' raised during send: %s",
                    channel.name(),
                    exc,
                )
                results[channel.name()] = False
        return results
