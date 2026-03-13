"""Create the pending_actions table for persisting the approval queue.

Stores RECOMMEND-tier actions awaiting operator approval. On startup,
rows with status='pending' are loaded back into the in-memory queue.
Resolved rows (approved/rejected/expired) serve as an audit trail.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import aiosqlite


async def migrate(db: aiosqlite.Connection) -> None:
    """Create pending_actions table with composite index."""
    await db.execute("""
        CREATE TABLE pending_actions (
            id          TEXT    PRIMARY KEY,
            event_id    TEXT    NOT NULL,
            action_json TEXT    NOT NULL,
            diagnosis   TEXT    NOT NULL,
            status      TEXT    NOT NULL DEFAULT 'pending',
            created_at  TEXT    NOT NULL,
            expires_at  TEXT    NOT NULL,
            entity_id   TEXT    NOT NULL DEFAULT '',
            severity    TEXT    NOT NULL DEFAULT '',
            source      TEXT    NOT NULL DEFAULT '',
            system      TEXT    NOT NULL DEFAULT ''
        )
    """)
    await db.execute("""
        CREATE INDEX idx_pending_actions_status_expires
        ON pending_actions (status, expires_at)
    """)
