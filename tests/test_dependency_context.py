"""Tests for dependency context extraction (gather_dependency_context)."""

from __future__ import annotations

from datetime import UTC, datetime

from oasisagent.engine.service_graph import ServiceGraph, gather_dependency_context
from oasisagent.models import TopologyEdge, TopologyNode

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _node(entity_id: str, entity_type: str = "service", host_ip: str | None = None) -> TopologyNode:
    return TopologyNode(
        entity_id=entity_id,
        entity_type=entity_type,
        display_name=entity_id,
        host_ip=host_ip,
        source="test",
        last_seen=datetime.now(UTC),
    )


def _edge(from_e: str, to_e: str, edge_type: str = "depends_on") -> TopologyEdge:
    return TopologyEdge(
        from_entity=from_e,
        to_entity=to_e,
        edge_type=edge_type,
        source="test",
        last_seen=datetime.now(UTC),
    )


def _build_graph(
    nodes: list[TopologyNode],
    edges: list[TopologyEdge],
) -> ServiceGraph:
    graph = ServiceGraph()
    for node in nodes:
        graph._nodes[node.entity_id] = node
    graph._edges = edges
    graph._rebuild_indexes()
    return graph


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestEmptyGraph:
    def test_entity_not_in_graph(self) -> None:
        graph = _build_graph([], [])
        ctx = gather_dependency_context("missing_entity", graph)

        assert ctx.entity_id == "missing_entity"
        assert ctx.entity_type is None
        assert ctx.upstream == []
        assert ctx.downstream == []
        assert ctx.same_host == []
        assert ctx.edges == []

    def test_entity_in_graph_no_edges(self) -> None:
        graph = _build_graph([_node("svc:a")], [])
        ctx = gather_dependency_context("svc:a", graph)

        assert ctx.entity_id == "svc:a"
        assert ctx.entity_type == "service"
        assert ctx.upstream == []
        assert ctx.downstream == []


class TestDirectUpstream:
    def test_single_upstream(self) -> None:
        """A --depends_on--> B: querying A should find B upstream."""
        graph = _build_graph(
            [_node("svc:a"), _node("host:b", "host")],
            [_edge("svc:a", "host:b", "depends_on")],
        )
        ctx = gather_dependency_context("svc:a", graph)

        assert len(ctx.upstream) == 1
        assert ctx.upstream[0].entity_id == "host:b"
        assert ctx.upstream[0].edge_type == "depends_on"
        assert ctx.upstream[0].depth == 1


class TestDirectDownstream:
    def test_single_downstream(self) -> None:
        """A --depends_on--> B: querying B should find A downstream."""
        graph = _build_graph(
            [_node("svc:a"), _node("host:b", "host")],
            [_edge("svc:a", "host:b", "depends_on")],
        )
        ctx = gather_dependency_context("host:b", graph)

        assert len(ctx.downstream) == 1
        assert ctx.downstream[0].entity_id == "svc:a"
        assert ctx.downstream[0].edge_type == "depends_on"
        assert ctx.downstream[0].depth == 1


class TestDepthControl:
    def test_depth_1_stops_at_direct(self) -> None:
        """Chain A→B→C, depth=1: only B visible from A."""
        graph = _build_graph(
            [_node("a"), _node("b"), _node("c")],
            [_edge("a", "b"), _edge("b", "c")],
        )
        ctx = gather_dependency_context("a", graph, depth=1)

        assert len(ctx.upstream) == 1
        assert ctx.upstream[0].entity_id == "b"

    def test_depth_2_traverses_chain(self) -> None:
        """Chain A→B→C, depth=2: both B and C visible from A."""
        graph = _build_graph(
            [_node("a"), _node("b"), _node("c")],
            [_edge("a", "b"), _edge("b", "c")],
        )
        ctx = gather_dependency_context("a", graph, depth=2)

        upstream_ids = {n.entity_id for n in ctx.upstream}
        assert upstream_ids == {"b", "c"}
        # Check depths
        depths = {n.entity_id: n.depth for n in ctx.upstream}
        assert depths["b"] == 1
        assert depths["c"] == 2

    def test_max_depth_capped_at_5(self) -> None:
        """depth=10 should behave like depth=5."""
        # Build a chain of 7 nodes: n0→n1→...→n6
        nodes = [_node(f"n{i}") for i in range(7)]
        edges = [_edge(f"n{i}", f"n{i+1}") for i in range(6)]
        graph = _build_graph(nodes, edges)

        ctx = gather_dependency_context("n0", graph, depth=10)

        # Should stop at 5 hops, so n1-n5 visible, n6 not
        upstream_ids = {n.entity_id for n in ctx.upstream}
        assert "n5" in upstream_ids
        assert "n6" not in upstream_ids

    def test_depth_0_clamped_to_1(self) -> None:
        """depth=0 should be clamped to 1."""
        graph = _build_graph(
            [_node("a"), _node("b"), _node("c")],
            [_edge("a", "b"), _edge("b", "c")],
        )
        ctx = gather_dependency_context("a", graph, depth=0)

        assert len(ctx.upstream) == 1
        assert ctx.upstream[0].entity_id == "b"


class TestSameHost:
    def test_same_host_entities(self) -> None:
        """Nodes with same host_ip appear in same_host."""
        graph = _build_graph(
            [
                _node("svc:mqtt", "service", "192.168.1.100"),
                _node("svc:zigbee", "service", "192.168.1.100"),
                _node("svc:other", "service", "192.168.1.200"),
            ],
            [],
        )
        ctx = gather_dependency_context("svc:mqtt", graph)

        assert len(ctx.same_host) == 1
        assert ctx.same_host[0].entity_id == "svc:zigbee"

    def test_no_host_ip_no_same_host(self) -> None:
        """Node without host_ip has empty same_host."""
        graph = _build_graph([_node("svc:a")], [])
        ctx = gather_dependency_context("svc:a", graph)

        assert ctx.same_host == []


class TestEdgesCollected:
    def test_subgraph_edges_included(self) -> None:
        """Edges connecting subgraph members are included."""
        graph = _build_graph(
            [_node("a"), _node("b"), _node("c"), _node("d")],
            [
                _edge("a", "b", "depends_on"),
                _edge("b", "c", "runs_on"),
                _edge("c", "d", "proxies_to"),  # d is outside depth=1 from a
            ],
        )
        ctx = gather_dependency_context("a", graph, depth=2)

        edge_pairs = {(e.from_entity, e.to_entity) for e in ctx.edges}
        assert ("a", "b") in edge_pairs
        assert ("b", "c") in edge_pairs


class TestMixedEdgeTypes:
    def test_runs_on_and_proxies_to_traversed(self) -> None:
        """Different edge types are all traversed."""
        graph = _build_graph(
            [_node("svc:app"), _node("host:rpi4", "host"), _node("proxy:npm", "proxy")],
            [
                _edge("svc:app", "host:rpi4", "runs_on"),
                _edge("svc:app", "proxy:npm", "proxies_to"),
            ],
        )
        ctx = gather_dependency_context("svc:app", graph)

        upstream_ids = {n.entity_id for n in ctx.upstream}
        assert upstream_ids == {"host:rpi4", "proxy:npm"}

    def test_forwards_to_traversed(self) -> None:
        """forwards_to edges are traversed."""
        graph = _build_graph(
            [_node("proxy:a"), _node("svc:b")],
            [_edge("proxy:a", "svc:b", "forwards_to")],
        )
        ctx = gather_dependency_context("proxy:a", graph)

        assert len(ctx.upstream) == 1
        assert ctx.upstream[0].entity_id == "svc:b"
        assert ctx.upstream[0].edge_type == "forwards_to"

    def test_resolves_via_traversed(self) -> None:
        """resolves_via edges are traversed."""
        graph = _build_graph(
            [_node("svc:a"), _node("dns:b")],
            [_edge("svc:a", "dns:b", "resolves_via")],
        )
        ctx = gather_dependency_context("svc:a", graph)

        assert len(ctx.upstream) == 1
        assert ctx.upstream[0].edge_type == "resolves_via"


class TestCycleHandling:
    def test_cycle_does_not_infinite_loop(self) -> None:
        """A→B→A cycle completes without infinite loop."""
        graph = _build_graph(
            [_node("a"), _node("b")],
            [_edge("a", "b"), _edge("b", "a")],
        )
        ctx = gather_dependency_context("a", graph, depth=5)

        # b is upstream (a→b), a is not re-added
        upstream_ids = {n.entity_id for n in ctx.upstream}
        assert upstream_ids == {"b"}

    def test_self_referencing_edge(self) -> None:
        """A→A edge doesn't cause issues."""
        graph = _build_graph(
            [_node("a"), _node("b")],
            [_edge("a", "a"), _edge("a", "b")],
        )
        ctx = gather_dependency_context("a", graph)

        upstream_ids = {n.entity_id for n in ctx.upstream}
        assert upstream_ids == {"b"}
