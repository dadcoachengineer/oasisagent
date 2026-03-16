"""Create the candidate_fixes table for the learning loop.

Tracks T2-suggested known fixes with dedup and verification counting.
YAML artifacts are written to disk; this table tracks operational
metadata (occurrence count, notification state).

Runtime settings (min_confidence, min_verified_count) come from the
``LearningConfig`` Pydantic model, not from the database.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import aiosqlite


async def migrate(db: aiosqlite.Connection) -> None:
    """Create learning loop table."""
    await db.execute("""
        CREATE TABLE candidate_fixes (
            match_hash     TEXT PRIMARY KEY,
            system         TEXT NOT NULL,
            event_type     TEXT NOT NULL,
            entity_pattern TEXT NOT NULL DEFAULT '',
            candidate_yaml TEXT NOT NULL,
            candidate_path TEXT NOT NULL DEFAULT '',
            confidence     REAL NOT NULL,
            verified_count INTEGER NOT NULL DEFAULT 1,
            first_seen     TEXT NOT NULL,
            last_seen      TEXT NOT NULL,
            notified       INTEGER NOT NULL DEFAULT 0
        )
    """)
    await db.execute("""
        CREATE INDEX idx_candidate_fixes_notify
        ON candidate_fixes (notified, verified_count)
    """)
