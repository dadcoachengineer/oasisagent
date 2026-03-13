"""Interactive notification channel ABC — bidirectional approval support.

Extends NotificationChannel with methods for sending approval requests
with interactive affordances (e.g., Telegram inline keyboards, Slack
Block Kit buttons) and listening for operator responses.

The orchestrator manages the listener lifecycle directly. The dispatcher
handles fan-out for approval request dispatch and message updates.
"""

from __future__ import annotations

from abc import abstractmethod
from typing import TYPE_CHECKING

from oasisagent.notifications.base import NotificationChannel

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from oasisagent.approval.pending import ApprovalDecision, PendingAction, PendingStatus


class InteractiveNotificationChannel(NotificationChannel):
    """Notification channel that supports interactive approval responses.

    Subclasses implement the full NotificationChannel interface (send, name,
    start, stop) plus the interactive methods below. The channel is responsible
    for tracking its own message references internally — the ABC does not
    expose message IDs to callers.
    """

    @abstractmethod
    async def send_approval_request(self, pending: PendingAction) -> None:
        """Send a message with approve/reject affordances.

        The channel should format the PendingAction into a rich message
        with interactive controls (buttons, reactions, etc.) and store
        any internal message reference needed for later updates.

        Args:
            pending: The action awaiting operator approval.
        """

    @abstractmethod
    async def start_listener(
        self,
        callback: Callable[[str, ApprovalDecision], Awaitable[None]],
    ) -> None:
        """Start listening for interactive approval responses.

        The callback receives (action_id, decision) when an operator
        interacts with an approval message. The orchestrator provides
        the callback and manages first-wins resolution.

        Args:
            callback: Async function called with (action_id, decision).
        """

    @abstractmethod
    async def stop_listener(self) -> None:
        """Stop the interactive listener gracefully.

        Separate from stop() so the channel can still send notifications
        after the listener is torn down (useful during shutdown drain).
        """

    async def update_approval_message(
        self, action_id: str, status: PendingStatus,
    ) -> None:
        """Update a previously sent approval message to reflect resolution.

        Called by the dispatcher when an action is resolved (approved,
        rejected, or expired) through any channel. Implementations should
        edit the original message to show the final status and remove
        interactive controls.

        Default no-op — channels that support message editing override this.

        Args:
            action_id: The ID of the resolved action.
            status: The final status (approved, rejected, expired).
        """
