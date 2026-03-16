"""Notification store — persists web UI notifications to SQLite.

Stores notifications for the in-app notification feed. Supports
pagination, severity filtering, action status updates for interactive
cards, and periodic pruning with archival.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sqlite3 import Row

    import aiosqlite

    from oasisagent.models import Notification

logger = logging.getLogger(__name__)

MAX_ROWS = 1000
PRUNE_BUFFER = 100


class NotificationStore:
    """SQLite-backed notification store for the web UI feed.

    All methods use the shared aiosqlite connection from app state.
    Pruning is decoupled from add() — the orchestrator calls prune()
    on a periodic timer.
    """

    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    async def add(
        self,
        notification: Notification,
        *,
        action_id: str | None = None,
        action_status: str | None = None,
    ) -> str:
        """Insert a notification row. Returns the notification ID."""
        await self._db.execute(
            "INSERT INTO notifications "
            "(id, event_id, severity, title, message, timestamp, "
            "action_id, action_status, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                notification.id,
                notification.event_id,
                notification.severity.value,
                notification.title,
                notification.message,
                notification.timestamp.isoformat(),
                action_id,
                action_status,
                json.dumps(notification.metadata, default=str),
            ),
        )
        await self._db.commit()
        return notification.id

    async def list_recent(
        self,
        limit: int = 50,
        offset: int = 0,
        severity: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return recent non-archived notifications, newest first."""
        where = "WHERE archived = 0"
        params: list[Any] = []
        if severity:
            where += " AND severity = ?"
            params.append(severity)

        params.extend([limit, offset])
        cursor = await self._db.execute(
            f"SELECT * FROM notifications {where} "
            "ORDER BY timestamp DESC LIMIT ? OFFSET ?",
            params,
        )
        rows = await cursor.fetchall()
        return [_row_to_dict(row) for row in rows]

    async def get(self, notification_id: str) -> dict[str, Any] | None:
        """Fetch a single notification by ID."""
        cursor = await self._db.execute(
            "SELECT * FROM notifications WHERE id = ?",
            (notification_id,),
        )
        row = await cursor.fetchone()
        return _row_to_dict(row) if row else None

    async def update_action_status(
        self, action_id: str, status: str,
    ) -> None:
        """Update the denormalized action_status for a given action_id."""
        await self._db.execute(
            "UPDATE notifications SET action_status = ? WHERE action_id = ?",
            (status, action_id),
        )
        await self._db.commit()

    async def count_unarchived(self) -> int:
        """Return the count of non-archived notifications."""
        cursor = await self._db.execute(
            "SELECT COUNT(*) FROM notifications WHERE archived = 0"
        )
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def count(self) -> int:
        """Return the total count of all notifications."""
        cursor = await self._db.execute("SELECT COUNT(*) FROM notifications")
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def prune(self) -> list[dict[str, Any]]:
        """Prune oldest rows when count exceeds MAX_ROWS + PRUNE_BUFFER.

        Marks excess rows as archived=1, returns them for InfluxDB archival.
        Only prunes when count > MAX_ROWS + PRUNE_BUFFER.
        """
        total = await self.count()
        if total <= MAX_ROWS + PRUNE_BUFFER:
            return []

        excess = total - MAX_ROWS
        cursor = await self._db.execute(
            "SELECT * FROM notifications WHERE archived = 0 "
            "ORDER BY timestamp ASC LIMIT ?",
            (excess,),
        )
        rows = await cursor.fetchall()
        archived = [_row_to_dict(row) for row in rows]

        if archived:
            ids = [r["id"] for r in archived]
            placeholders = ",".join("?" * len(ids))
            await self._db.execute(
                f"UPDATE notifications SET archived = 1 "
                f"WHERE id IN ({placeholders})",
                ids,
            )
            await self._db.commit()
            logger.info("Pruned %d notifications (archived for InfluxDB)", len(archived))

        return archived


def _row_to_dict(row: Row) -> dict[str, Any]:
    """Convert a sqlite3.Row to a plain dict with parsed metadata."""
    d = dict(row)
    if isinstance(d.get("metadata"), str):
        d["metadata"] = json.loads(d["metadata"])
    return d
