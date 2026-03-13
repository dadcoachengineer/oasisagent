"""Web notification channel — in-app feed with SSE push and interactive approvals.

Always-enabled channel that writes to NotificationStore and pushes real-time
updates to SSE subscribers. Implements InteractiveNotificationChannel for
inline approve/reject on RECOMMEND-tier actions.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

from oasisagent.notifications.interactive import InteractiveNotificationChannel

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from oasisagent.approval.pending import ApprovalDecision, PendingAction, PendingStatus
    from oasisagent.db.notification_store import NotificationStore
    from oasisagent.models import Notification

logger = logging.getLogger(__name__)


class WebNotificationChannel(InteractiveNotificationChannel):
    """In-app notification channel with SSE fan-out.

    Writes every notification to SQLite via NotificationStore and
    pushes SSE events to connected browser tabs. Interactive approval
    cards are supported through action_id linkage.
    """

    def __init__(self, store: NotificationStore) -> None:
        self._store = store
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._approval_callback: Callable[[str, ApprovalDecision], Awaitable[None]] | None = None

    def name(self) -> str:
        return "web"

    async def send(self, notification: Notification) -> bool:
        """Write to store and push to SSE subscribers."""
        try:
            await self._store.add(notification)
            self._push_sse({
                "event": "notification",
                "data": _notification_to_sse(notification),
            })
            return True
        except Exception:
            logger.exception("Failed to write web notification")
            return False

    async def send_approval_request(self, pending: PendingAction) -> None:
        """Write an interactive notification linked to a pending action."""
        from oasisagent.models import Notification, Severity

        severity = Severity(pending.severity) if pending.severity else Severity.WARNING
        notification = Notification(
            event_id=pending.event_id,
            severity=severity,
            title=f"[APPROVAL] {pending.action.description[:80]}",
            message=(
                f"Action: {pending.action.operation} via {pending.action.handler}\n"
                f"Diagnosis: {pending.diagnosis}\n"
                f"Entity: {pending.entity_id}"
            ),
            metadata={
                "entity_id": pending.entity_id,
                "handler": pending.action.handler,
                "source": pending.source,
            },
        )

        await self._store.add(
            notification,
            action_id=pending.id,
            action_status="pending",
        )

        self._push_sse({
            "event": "notification",
            "data": _notification_to_sse(
                notification,
                action_id=pending.id,
                action_status="pending",
            ),
        })

    async def start_listener(
        self,
        callback: Callable[[str, ApprovalDecision], Awaitable[None]],
    ) -> None:
        """Store the approval callback for web UI approve/reject routes."""
        self._approval_callback = callback

    async def stop_listener(self) -> None:
        """Clear the approval callback."""
        self._approval_callback = None

    async def update_approval_message(
        self, action_id: str, status: PendingStatus,
    ) -> None:
        """Update the denormalized action_status and push SSE update."""
        await self._store.update_action_status(action_id, status.value)
        self._push_sse({
            "event": "action_update",
            "data": json.dumps({
                "action_id": action_id,
                "action_status": status.value,
            }),
        })

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        """Create a new SSE subscriber queue."""
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=50)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        """Remove an SSE subscriber queue."""
        self._subscribers.discard(queue)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def _push_sse(self, message: dict[str, Any]) -> None:
        """Fan out a message to all SSE subscribers. Drop for slow consumers."""
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                logger.debug(
                    "SSE subscriber queue full, dropping message"
                )


def _notification_to_sse(
    notification: Notification,
    *,
    action_id: str | None = None,
    action_status: str | None = None,
) -> str:
    """Serialize a notification to JSON for SSE transmission."""
    return json.dumps({
        "id": notification.id,
        "event_id": notification.event_id,
        "severity": notification.severity.value,
        "title": notification.title,
        "message": notification.message,
        "timestamp": notification.timestamp.isoformat(),
        "action_id": action_id,
        "action_status": action_status,
        "metadata": notification.metadata,
    }, default=str)
