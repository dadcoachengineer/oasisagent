"""SQLite persistence for the service topology graph (#218).

Nodes and edges are auto-discovered by adapters and can be manually
edited via the UI. The ``manually_edited`` flag protects operator
overrides from being clobbered by auto-discovery.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from oasisagent.models import TopologyEdge, TopologyNode

if TYPE_CHECKING:
    import aiosqlite

logger = logging.getLogger(__name__)


class TopologyStore:
    """CRUD operations for topology_nodes and topology_edges tables."""

    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    # -------------------------------------------------------------------
    # Nodes
    # -------------------------------------------------------------------

    async def list_nodes(self) -> list[TopologyNode]:
        """Return all topology nodes."""
        cursor = await self._db.execute(
            "SELECT entity_id, entity_type, display_name, host_ip, "
            "source, manually_edited, last_seen, metadata "
            "FROM topology_nodes ORDER BY entity_id"
        )
        rows = await cursor.fetchall()
        return [self._row_to_node(row) for row in rows]

    async def get_node(self, entity_id: str) -> TopologyNode | None:
        """Return a single node by entity_id, or None."""
        cursor = await self._db.execute(
            "SELECT entity_id, entity_type, display_name, host_ip, "
            "source, manually_edited, last_seen, metadata "
            "FROM topology_nodes WHERE entity_id = ?",
            (entity_id,),
        )
        row = await cursor.fetchone()
        return self._row_to_node(row) if row else None

    async def upsert_node(self, node: TopologyNode) -> None:
        """Insert or update a node. Respects manually_edited flag."""
        existing = await self.get_node(node.entity_id)
        now_iso = datetime.now(UTC).isoformat()

        if existing is None:
            await self._db.execute(
                "INSERT INTO topology_nodes "
                "(entity_id, entity_type, display_name, host_ip, "
                "source, manually_edited, last_seen, metadata) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    node.entity_id,
                    node.entity_type,
                    node.display_name,
                    node.host_ip,
                    node.source,
                    1 if node.manually_edited else 0,
                    now_iso,
                    json.dumps(node.metadata, default=str),
                ),
            )
        elif existing.manually_edited and not node.manually_edited:
            # Auto-discovery: only update last_seen for manually edited nodes
            await self._db.execute(
                "UPDATE topology_nodes SET last_seen = ? WHERE entity_id = ?",
                (now_iso, node.entity_id),
            )
        else:
            await self._db.execute(
                "UPDATE topology_nodes SET entity_type = ?, display_name = ?, "
                "host_ip = ?, source = ?, manually_edited = ?, "
                "last_seen = ?, metadata = ? WHERE entity_id = ?",
                (
                    node.entity_type,
                    node.display_name,
                    node.host_ip,
                    node.source,
                    1 if node.manually_edited else 0,
                    now_iso,
                    json.dumps(node.metadata, default=str),
                    node.entity_id,
                ),
            )
        await self._db.commit()

    async def delete_node(self, entity_id: str) -> None:
        """Delete a node and its edges."""
        await self._db.execute(
            "DELETE FROM topology_edges "
            "WHERE from_entity = ? OR to_entity = ?",
            (entity_id, entity_id),
        )
        await self._db.execute(
            "DELETE FROM topology_nodes WHERE entity_id = ?",
            (entity_id,),
        )
        await self._db.commit()

    # -------------------------------------------------------------------
    # Edges
    # -------------------------------------------------------------------

    async def list_edges(self) -> list[TopologyEdge]:
        """Return all topology edges."""
        cursor = await self._db.execute(
            "SELECT from_entity, to_entity, edge_type, "
            "source, manually_edited, last_seen "
            "FROM topology_edges ORDER BY from_entity, to_entity"
        )
        rows = await cursor.fetchall()
        return [self._row_to_edge(row) for row in rows]

    async def upsert_edge(self, edge: TopologyEdge) -> None:
        """Insert or update an edge. Respects manually_edited flag."""
        now_iso = datetime.now(UTC).isoformat()
        cursor = await self._db.execute(
            "SELECT manually_edited FROM topology_edges "
            "WHERE from_entity = ? AND to_entity = ? AND edge_type = ?",
            (edge.from_entity, edge.to_entity, edge.edge_type),
        )
        existing = await cursor.fetchone()

        if existing is None:
            await self._db.execute(
                "INSERT INTO topology_edges "
                "(from_entity, to_entity, edge_type, source, "
                "manually_edited, last_seen) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    edge.from_entity,
                    edge.to_entity,
                    edge.edge_type,
                    edge.source,
                    1 if edge.manually_edited else 0,
                    now_iso,
                ),
            )
        elif existing["manually_edited"] and not edge.manually_edited:
            await self._db.execute(
                "UPDATE topology_edges SET last_seen = ? "
                "WHERE from_entity = ? AND to_entity = ? AND edge_type = ?",
                (now_iso, edge.from_entity, edge.to_entity, edge.edge_type),
            )
        else:
            await self._db.execute(
                "UPDATE topology_edges SET source = ?, "
                "manually_edited = ?, last_seen = ? "
                "WHERE from_entity = ? AND to_entity = ? AND edge_type = ?",
                (
                    edge.source,
                    1 if edge.manually_edited else 0,
                    now_iso,
                    edge.from_entity,
                    edge.to_entity,
                    edge.edge_type,
                ),
            )
        await self._db.commit()

    async def delete_edge(
        self, from_entity: str, to_entity: str, edge_type: str
    ) -> None:
        """Delete a specific edge."""
        await self._db.execute(
            "DELETE FROM topology_edges "
            "WHERE from_entity = ? AND to_entity = ? AND edge_type = ?",
            (from_entity, to_entity, edge_type),
        )
        await self._db.commit()

    # -------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------

    @staticmethod
    def _row_to_node(row: Any) -> TopologyNode:
        """Convert a database row to a TopologyNode."""
        last_seen_str = row["last_seen"]
        last_seen = (
            datetime.fromisoformat(last_seen_str)
            if last_seen_str
            else None
        )
        metadata_str = row["metadata"] or "{}"
        return TopologyNode(
            entity_id=row["entity_id"],
            entity_type=row["entity_type"],
            display_name=row["display_name"] or "",
            host_ip=row["host_ip"],
            source=row["source"] or "manual",
            manually_edited=bool(row["manually_edited"]),
            last_seen=last_seen,
            metadata=json.loads(metadata_str),
        )

    @staticmethod
    def _row_to_edge(row: Any) -> TopologyEdge:
        """Convert a database row to a TopologyEdge."""
        last_seen_str = row["last_seen"]
        last_seen = (
            datetime.fromisoformat(last_seen_str)
            if last_seen_str
            else None
        )
        return TopologyEdge(
            from_entity=row["from_entity"],
            to_entity=row["to_entity"],
            edge_type=row["edge_type"],
            source=row["source"] or "manual",
            manually_edited=bool(row["manually_edited"]),
            last_seen=last_seen,
        )
