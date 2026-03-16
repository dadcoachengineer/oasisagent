"""Add topology_nodes and topology_edges tables for service graph (#218).

Supports auto-discovery from adapters and manual editing via the UI.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import aiosqlite


async def migrate(db: aiosqlite.Connection) -> None:
    """Create topology tables."""
    await db.execute("""
        CREATE TABLE topology_nodes (
            entity_id       TEXT PRIMARY KEY,
            entity_type     TEXT NOT NULL,
            display_name    TEXT DEFAULT '',
            host_ip         TEXT,
            source          TEXT DEFAULT 'manual',
            manually_edited INTEGER DEFAULT 0,
            last_seen       TEXT,
            metadata        TEXT DEFAULT '{}'
        )
    """)
    await db.execute("""
        CREATE TABLE topology_edges (
            id              INTEGER PRIMARY KEY,
            from_entity     TEXT NOT NULL REFERENCES topology_nodes(entity_id),
            to_entity       TEXT NOT NULL REFERENCES topology_nodes(entity_id),
            edge_type       TEXT NOT NULL,
            source          TEXT DEFAULT 'manual',
            manually_edited INTEGER DEFAULT 0,
            last_seen       TEXT,
            UNIQUE(from_entity, to_entity, edge_type)
        )
    """)
