"""Add correlation_clusters and cluster_events tables (#218 M3).

Stores cross-domain correlation clusters — groups of events from
different systems that are part of the same incident.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import aiosqlite


async def migrate(db: aiosqlite.Connection) -> None:
    """Create correlation cluster tables."""
    await db.execute("""
        CREATE TABLE correlation_clusters (
            id              TEXT PRIMARY KEY,
            created_at      TEXT NOT NULL,
            updated_at      TEXT NOT NULL,
            leader_event_id TEXT NOT NULL,
            diagnosis       TEXT DEFAULT '',
            rule_type       TEXT DEFAULT '',
            event_count     INTEGER DEFAULT 1
        )
    """)
    await db.execute("""
        CREATE TABLE cluster_events (
            id              INTEGER PRIMARY KEY,
            cluster_id      TEXT NOT NULL REFERENCES correlation_clusters(id),
            event_id        TEXT NOT NULL,
            entity_id       TEXT NOT NULL,
            source          TEXT NOT NULL,
            system          TEXT NOT NULL,
            severity        TEXT NOT NULL,
            timestamp       TEXT NOT NULL,
            matched_rule    TEXT DEFAULT '',
            UNIQUE(cluster_id, event_id)
        )
    """)
    await db.execute(
        "CREATE INDEX idx_cluster_events_cluster ON cluster_events(cluster_id)"
    )
    await db.execute(
        "CREATE INDEX idx_cluster_events_event ON cluster_events(event_id)"
    )
