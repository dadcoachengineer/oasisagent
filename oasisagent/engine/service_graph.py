"""In-memory service topology graph for cross-domain correlation (#218).

The ServiceGraph is loaded from SQLite on startup, updated by adapter
discovery, and queried by the cross-domain correlator to find related
services (same host, dependency chain, same subnet).
"""

from __future__ import annotations

import ipaddress
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from oasisagent.models import TopologyDiff, TopologyEdge, TopologyNode

if TYPE_CHECKING:
    from oasisagent.db.topology_store import TopologyStore

logger = logging.getLogger(__name__)


class ServiceGraph:
    """In-memory graph of services, hosts, and their relationships.

    Built from SQLite topology tables. Provides fast lookups for the
    cross-domain correlator without hitting the database on every event.
    """

    def __init__(self) -> None:
        self._nodes: dict[str, TopologyNode] = {}
        self._edges: list[TopologyEdge] = []
        # Derived indexes for fast lookup
        self._ip_to_entities: dict[str, list[str]] = {}
        self._entity_to_deps: dict[str, list[str]] = {}  # entity -> [depends_on]
        self._entity_to_dependents: dict[str, list[str]] = {}  # entity -> [depended_on_by]

    async def load_from_db(self, store: TopologyStore) -> None:
        """Load all nodes and edges from the topology store."""
        nodes = await store.list_nodes()
        edges = await store.list_edges()

        self._nodes.clear()
        self._edges.clear()

        for node in nodes:
            self._nodes[node.entity_id] = node

        self._edges = edges
        self._rebuild_indexes()

        logger.info(
            "ServiceGraph loaded: %d nodes, %d edges",
            len(self._nodes),
            len(self._edges),
        )

    async def merge_discovered(
        self,
        nodes: list[TopologyNode],
        edges: list[TopologyEdge],
        store: TopologyStore,
    ) -> list[TopologyDiff]:
        """Merge auto-discovered nodes/edges into the graph and persist.

        Returns a list of diffs (added/updated/stale) for the UI.
        Respects manually_edited: only updates last_seen for those.
        """
        diffs: list[TopologyDiff] = []

        for node in nodes:
            existing = self._nodes.get(node.entity_id)
            if existing is None:
                diffs.append(TopologyDiff(
                    action="added",
                    entity_type="node",
                    entity_id=node.entity_id,
                    details=f"{node.entity_type}: {node.display_name}",
                ))
            elif existing.manually_edited and not node.manually_edited:
                diffs.append(TopologyDiff(
                    action="updated",
                    entity_type="node",
                    entity_id=node.entity_id,
                    details="last_seen updated (manually edited, preserved)",
                ))
            elif (
                existing.host_ip != node.host_ip
                or existing.display_name != node.display_name
                or existing.entity_type != node.entity_type
            ):
                diffs.append(TopologyDiff(
                    action="updated",
                    entity_type="node",
                    entity_id=node.entity_id,
                    details=f"{node.entity_type}: {node.display_name}",
                ))

            await store.upsert_node(node)
            # Update in-memory copy
            refreshed = await store.get_node(node.entity_id)
            if refreshed:
                self._nodes[node.entity_id] = refreshed

        for edge in edges:
            edge_key = f"{edge.from_entity}->{edge.to_entity}"
            existing_edge = self._find_edge(
                edge.from_entity, edge.to_entity, edge.edge_type
            )
            if existing_edge is None:
                diffs.append(TopologyDiff(
                    action="added",
                    entity_type="edge",
                    entity_id=edge_key,
                    details=f"{edge.edge_type}",
                ))
            await store.upsert_edge(edge)

        # Reload edges from DB to stay in sync
        self._edges = await store.list_edges()
        self._rebuild_indexes()

        return diffs

    def detect_stale(
        self, max_missed_cycles: int = 3, cycle_seconds: int = 300,
    ) -> list[TopologyDiff]:
        """Find nodes not seen for more than max_missed_cycles * cycle_seconds.

        Returns diffs with action="stale". Does not delete — the operator
        decides via the UI.
        """
        stale: list[TopologyDiff] = []
        cutoff = datetime.now(UTC).timestamp() - (max_missed_cycles * cycle_seconds)

        for node in self._nodes.values():
            if node.last_seen is None:
                continue
            if node.last_seen.timestamp() < cutoff:
                stale.append(TopologyDiff(
                    action="stale",
                    entity_type="node",
                    entity_id=node.entity_id,
                    details=f"Last seen: {node.last_seen.isoformat()}",
                ))

        return stale

    # -------------------------------------------------------------------
    # Query methods used by the cross-domain correlator
    # -------------------------------------------------------------------

    def services_on_host(self, ip: str) -> list[str]:
        """Return entity_ids of services on the given host IP. O(1)."""
        return list(self._ip_to_entities.get(ip, []))

    def depends_on(self, entity_id: str) -> list[str]:
        """Return entity_ids that this entity depends on."""
        return list(self._entity_to_deps.get(entity_id, []))

    def dependents_of(self, entity_id: str) -> list[str]:
        """Return entity_ids that depend on this entity."""
        return list(self._entity_to_dependents.get(entity_id, []))

    def host_for_service(self, service_id: str) -> str | None:
        """Return the host IP for a service, or None."""
        node = self._nodes.get(service_id)
        if node and node.host_ip:
            return node.host_ip
        return None

    def subnet_for_ip(self, ip: str) -> str | None:
        """Return the /24 subnet for an IP address."""
        try:
            addr = ipaddress.ip_address(ip)
            network = ipaddress.ip_network(f"{addr}/24", strict=False)
            return str(network)
        except ValueError:
            return None

    def devices_serving_subnet(self, subnet: str) -> list[str]:
        """Return network device entity_ids that serve the given subnet."""
        try:
            target_net = ipaddress.ip_network(subnet, strict=False)
        except ValueError:
            return []

        result: list[str] = []
        for entity_id, node in self._nodes.items():
            if node.entity_type != "network_device":
                continue
            if node.host_ip:
                try:
                    if ipaddress.ip_address(node.host_ip) in target_net:
                        result.append(entity_id)
                except ValueError:
                    continue
        return result

    def all_nodes(self) -> list[TopologyNode]:
        """Return all nodes in the graph."""
        return list(self._nodes.values())

    def get_node(self, entity_id: str) -> TopologyNode | None:
        """Return a specific node, or None."""
        return self._nodes.get(entity_id)

    def to_d3_json(self) -> dict[str, Any]:
        """Export graph as D3 force-directed JSON.

        Returns:
            {"nodes": [...], "links": [...]}
        """
        nodes = [
            {
                "id": n.entity_id,
                "type": n.entity_type,
                "name": n.display_name or n.entity_id,
                "ip": n.host_ip,
                "source": n.source,
                "manually_edited": n.manually_edited,
            }
            for n in self._nodes.values()
        ]
        links = [
            {
                "source": e.from_entity,
                "target": e.to_entity,
                "type": e.edge_type,
            }
            for e in self._edges
            # Only include edges where both endpoints exist
            if e.from_entity in self._nodes and e.to_entity in self._nodes
        ]
        return {"nodes": nodes, "links": links}

    # -------------------------------------------------------------------
    # Internal
    # -------------------------------------------------------------------

    def _rebuild_indexes(self) -> None:
        """Rebuild derived indexes from current nodes/edges."""
        self._ip_to_entities.clear()
        self._entity_to_deps.clear()
        self._entity_to_dependents.clear()

        for entity_id, node in self._nodes.items():
            if node.host_ip:
                self._ip_to_entities.setdefault(node.host_ip, []).append(entity_id)

        for edge in self._edges:
            if edge.edge_type in ("depends_on", "proxies_to", "runs_on", "connects_via"):
                self._entity_to_deps.setdefault(edge.from_entity, []).append(
                    edge.to_entity
                )
                self._entity_to_dependents.setdefault(edge.to_entity, []).append(
                    edge.from_entity
                )

    def _find_edge(
        self, from_entity: str, to_entity: str, edge_type: str
    ) -> TopologyEdge | None:
        """Find an edge by its composite key."""
        for edge in self._edges:
            if (
                edge.from_entity == from_entity
                and edge.to_entity == to_entity
                and edge.edge_type == edge_type
            ):
                return edge
        return None
