"""Create the stats table for persisting dashboard counters.

Simple key-value store for scalar metrics (events_processed,
actions_taken, errors). Values survive process restarts.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import aiosqlite


async def migrate(db: aiosqlite.Connection) -> None:
    """Create stats key-value table."""
    await db.execute("""
        CREATE TABLE stats (
            key   TEXT PRIMARY KEY,
            value INTEGER NOT NULL DEFAULT 0
        )
    """)
    # Seed with known keys so UPSERTs always hit existing rows
    await db.executemany(
        "INSERT INTO stats (key, value) VALUES (?, 0)",
        [("events_processed",), ("actions_taken",), ("errors",)],
    )
