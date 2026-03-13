"""Create the notifications table for the web notification feed.

Stores all notifications dispatched through OasisAgent for display
in the web UI feed. Supports interactive approve/reject for
RECOMMEND-tier actions via denormalized action_status.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import aiosqlite


async def migrate(db: aiosqlite.Connection) -> None:
    """Create notifications table and indexes."""
    await db.execute("""
        CREATE TABLE IF NOT EXISTS notifications (
            id              TEXT PRIMARY KEY,
            event_id        TEXT,
            severity        TEXT NOT NULL DEFAULT 'info',
            title           TEXT NOT NULL,
            message         TEXT NOT NULL,
            timestamp       TEXT NOT NULL,
            action_id       TEXT,
            action_status   TEXT,
            archived        INTEGER NOT NULL DEFAULT 0,
            metadata        TEXT NOT NULL DEFAULT '{}'
        )
    """)
    await db.execute(
        "CREATE INDEX idx_notifications_timestamp "
        "ON notifications(timestamp DESC)"
    )
    await db.execute(
        "CREATE INDEX idx_notifications_archived "
        "ON notifications(archived)"
    )
    await db.execute(
        "CREATE INDEX idx_notifications_action_id "
        "ON notifications(action_id)"
    )
