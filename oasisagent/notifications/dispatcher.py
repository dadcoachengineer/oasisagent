"""Notification dispatcher — fans out notifications to all enabled channels.

The dispatcher routes notifications and approval requests to registered
channels. Interactive channels (those implementing InteractiveNotificationChannel)
receive approval requests and message updates in addition to standard notifications.

The dispatcher handles fan-out routing only. The orchestrator manages
channel and listener lifecycles directly.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from oasisagent.notifications.interactive import InteractiveNotificationChannel

if TYPE_CHECKING:
    from oasisagent.approval.pending import PendingAction, PendingStatus
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

    @property
    def channels(self) -> list[NotificationChannel]:
        """All registered notification channels."""
        return list(self._channels)

    @property
    def interactive_channels(self) -> list[InteractiveNotificationChannel]:
        """All registered interactive notification channels."""
        return [
            ch for ch in self._channels
            if isinstance(ch, InteractiveNotificationChannel)
        ]

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

    async def dispatch_approval_request(self, pending: PendingAction) -> dict[str, bool]:
        """Send an approval request to all interactive channels.

        Non-interactive channels are skipped. Returns a dict mapping
        channel name → success boolean.
        """
        results: dict[str, bool] = {}
        for channel in self.interactive_channels:
            try:
                await channel.send_approval_request(pending)
                results[channel.name()] = True
            except Exception as exc:
                logger.warning(
                    "Interactive channel '%s' failed to send approval request: %s",
                    channel.name(),
                    exc,
                )
                results[channel.name()] = False
        return results

    async def update_approval_messages(
        self, action_id: str, status: PendingStatus,
    ) -> None:
        """Notify all interactive channels that an action has been resolved.

        Each channel updates its previously sent approval message (e.g.,
        editing a Telegram message to show "Approved" and removing buttons).
        Failures are logged, not raised.
        """
        for channel in self.interactive_channels:
            try:
                await channel.update_approval_message(action_id, status)
            except Exception as exc:
                logger.warning(
                    "Interactive channel '%s' failed to update approval message: %s",
                    channel.name(),
                    exc,
                )
