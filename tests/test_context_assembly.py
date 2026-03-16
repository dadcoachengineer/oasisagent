"""Tests for multi-handler context assembly."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

from oasisagent.engine.context_assembly import (
    _handler_for_node,
    gather_multi_handler_context,
)
from oasisagent.engine.service_graph import ServiceGraph
from oasisagent.models import (
    DependencyContext,
    DependencyNode,
    Event,
    EventMetadata,
    Severity,
    TopologyNode,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(**overrides: Any) -> Event:
    defaults: dict[str, Any] = {
        "source": "test",
        "system": "homeassistant",
        "event_type": "integration_failure",
        "entity_id": "sensor.temperature",
        "severity": Severity.ERROR,
        "timestamp": datetime.now(UTC),
        "payload": {},
        "metadata": EventMetadata(),
    }
    defaults.update(overrides)
    return Event(**defaults)


def _make_dep_node(
    entity_id: str,
    entity_type: str = "service",
    edge_type: str = "depends_on",
    depth: int = 1,
) -> DependencyNode:
    return DependencyNode(
        entity_id=entity_id,
        entity_type=entity_type,
        display_name=entity_id,
        edge_type=edge_type,
        depth=depth,
    )


def _make_handler(
    name: str,
    context: dict[str, Any] | None = None,
    entity_context: dict[str, Any] | None = None,
) -> AsyncMock:
    mock = AsyncMock()
    mock.name.return_value = name
    mock.get_context.return_value = context or {"handler": name}
    mock.get_context_for_entity.return_value = entity_context or {"entity": name}
    return mock


def _make_graph_with_source(
    entity_id: str, source: str,
) -> ServiceGraph:
    graph = ServiceGraph()
    graph._nodes[entity_id] = TopologyNode(
        entity_id=entity_id,
        entity_type="service",
        display_name=entity_id,
        source=source,
    )
    return graph


def _empty_graph() -> ServiceGraph:
    return ServiceGraph()


# ---------------------------------------------------------------------------
# _handler_for_node tests
# ---------------------------------------------------------------------------


class TestHandlerForNode:
    def test_auto_source(self) -> None:
        """source='auto:portainer' -> portainer handler."""
        graph = _make_graph_with_source("portainer:1/zigbee2mqtt", "auto:portainer")
        node = _make_dep_node("portainer:1/zigbee2mqtt")
        handlers = {"portainer": _make_handler("portainer")}

        result = _handler_for_node(node, handlers, graph)

        assert result == "portainer"

    def test_entity_prefix(self) -> None:
        """entity_id='proxmox:100' -> proxmox handler."""
        graph = _empty_graph()
        node = _make_dep_node("proxmox:100")
        handlers = {"proxmox": _make_handler("proxmox")}

        result = _handler_for_node(node, handlers, graph)

        assert result == "proxmox"

    def test_ha_entity(self) -> None:
        """entity_id='sensor.temp' -> homeassistant handler."""
        graph = _empty_graph()
        node = _make_dep_node("sensor.temp")
        handlers = {"homeassistant": _make_handler("homeassistant")}

        result = _handler_for_node(node, handlers, graph)

        assert result == "homeassistant"

    def test_manual_unknown(self) -> None:
        """source='manual', unknown prefix -> None."""
        graph = _make_graph_with_source("unknown_thing", "manual")
        node = _make_dep_node("unknown_thing")
        handlers = {"homeassistant": _make_handler("homeassistant")}

        result = _handler_for_node(node, handlers, graph)

        assert result is None

    def test_auto_source_handler_not_registered(self) -> None:
        """auto:portainer source but portainer handler not registered -> fallback."""
        graph = _make_graph_with_source("portainer:1/mqtt", "auto:portainer")
        node = _make_dep_node("portainer:1/mqtt")
        # No portainer handler, but entity_id prefix "portainer" won't match either
        handlers = {"homeassistant": _make_handler("homeassistant")}

        result = _handler_for_node(node, handlers, graph)

        assert result is None


# ---------------------------------------------------------------------------
# gather_multi_handler_context tests
# ---------------------------------------------------------------------------


class TestGatherMultiHandlerContext:
    async def test_primary_context(self) -> None:
        """Primary handler.get_context() called with original event."""
        ha = _make_handler("homeassistant", context={"entity_state": "unavailable"})
        handlers = {"homeassistant": ha}
        event = _make_event()
        graph = _empty_graph()

        result = await gather_multi_handler_context(event, None, handlers, graph)

        assert result["primary"] == {"entity_state": "unavailable"}
        ha.get_context.assert_awaited_once_with(event)

    async def test_dependency_contexts_concurrent(self) -> None:
        """Two dependency handlers called via get_context_for_entity()."""
        ha = _make_handler("homeassistant", entity_context={"state": "ok"})
        portainer = _make_handler("portainer", entity_context={"status": "running"})
        handlers = {"homeassistant": ha, "portainer": portainer}
        event = _make_event()

        dep_ctx = DependencyContext(
            entity_id="sensor.temperature",
            upstream=[_make_dep_node("portainer:1/mqtt")],
            downstream=[_make_dep_node("sensor.humidity")],
        )
        graph = _empty_graph()
        # Add topology nodes so handler resolution works
        graph._nodes["portainer:1/mqtt"] = TopologyNode(
            entity_id="portainer:1/mqtt",
            entity_type="service",
            display_name="mqtt",
            source="auto:portainer",
        )

        result = await gather_multi_handler_context(event, dep_ctx, handlers, graph)

        assert "primary" in result
        assert "dependency:portainer:portainer:1/mqtt" in result
        assert "dependency:homeassistant:sensor.humidity" in result
        portainer.get_context_for_entity.assert_awaited_once_with("portainer:1/mqtt")
        ha.get_context_for_entity.assert_awaited_once_with("sensor.humidity")

    async def test_handler_timeout_swallowed(self) -> None:
        """Handler that takes >5s -> context omitted, no error."""
        async def slow_context(entity_id: str) -> dict[str, Any]:
            await asyncio.sleep(10)
            return {"should": "not appear"}

        ha = _make_handler("homeassistant")
        ha.get_context_for_entity = slow_context
        handlers = {"homeassistant": ha}
        event = _make_event()

        dep_ctx = DependencyContext(
            entity_id="sensor.temperature",
            upstream=[_make_dep_node("sensor.humidity")],
        )

        result = await gather_multi_handler_context(
            event, dep_ctx, handlers, _empty_graph(),
        )

        # Primary should be present, slow dependency should be absent
        assert "primary" in result
        assert "dependency:homeassistant:sensor.humidity" not in result

    async def test_handler_error_swallowed(self) -> None:
        """Handler raises -> context omitted, no error."""
        ha = _make_handler("homeassistant")
        ha.get_context_for_entity.side_effect = RuntimeError("API down")
        handlers = {"homeassistant": ha}
        event = _make_event()

        dep_ctx = DependencyContext(
            entity_id="sensor.temperature",
            upstream=[_make_dep_node("sensor.humidity")],
        )

        result = await gather_multi_handler_context(
            event, dep_ctx, handlers, _empty_graph(),
        )

        assert "primary" in result
        assert "dependency:homeassistant:sensor.humidity" not in result

    async def test_no_dependency_ctx_returns_primary_only(self) -> None:
        """dep_ctx=None -> only primary."""
        ha = _make_handler("homeassistant")
        handlers = {"homeassistant": ha}
        event = _make_event()

        result = await gather_multi_handler_context(
            event, None, handlers, _empty_graph(),
        )

        assert "primary" in result
        assert len(result) == 1

    async def test_dedup_per_handler_per_entity(self) -> None:
        """Same handler+entity_id not called twice."""
        ha = _make_handler("homeassistant", entity_context={"state": "ok"})
        handlers = {"homeassistant": ha}
        event = _make_event()

        dup_node = _make_dep_node("sensor.humidity")
        dep_ctx = DependencyContext(
            entity_id="sensor.temperature",
            upstream=[dup_node],
            downstream=[dup_node],  # Same node appears in both lists
        )

        result = await gather_multi_handler_context(
            event, dep_ctx, handlers, _empty_graph(),
        )

        # get_context_for_entity should only be called once for sensor.humidity
        ha.get_context_for_entity.assert_awaited_once_with("sensor.humidity")
        assert "dependency:homeassistant:sensor.humidity" in result

    async def test_same_system_different_entities_both_called(self) -> None:
        """Two portainer entities -> two calls."""
        portainer = _make_handler("portainer", entity_context={"status": "ok"})
        handlers = {"portainer": portainer}
        event = _make_event(system="portainer", entity_id="portainer:1/mqtt")

        dep_ctx = DependencyContext(
            entity_id="portainer:1/mqtt",
            downstream=[
                _make_dep_node("portainer:1/zigbee2mqtt"),
                _make_dep_node("portainer:1/frigate"),
            ],
        )

        await gather_multi_handler_context(
            event, dep_ctx, handlers, _empty_graph(),
        )

        assert portainer.get_context_for_entity.await_count == 2

    async def test_no_handlers(self) -> None:
        """Empty handlers dict -> empty context."""
        event = _make_event()
        result = await gather_multi_handler_context(
            event, None, {}, _empty_graph(),
        )
        assert result == {}

    async def test_primary_handler_missing(self) -> None:
        """No handler for event.system -> no primary, deps still work."""
        ha = _make_handler("homeassistant", entity_context={"state": "ok"})
        handlers = {"homeassistant": ha}
        event = _make_event(system="unknown_system")

        dep_ctx = DependencyContext(
            entity_id="sensor.temperature",
            upstream=[_make_dep_node("sensor.humidity")],
        )

        result = await gather_multi_handler_context(
            event, dep_ctx, handlers, _empty_graph(),
        )

        assert "primary" not in result
        assert "dependency:homeassistant:sensor.humidity" in result

    async def test_primary_error_swallowed(self) -> None:
        """Primary handler error -> no primary, deps still work."""
        ha = _make_handler("homeassistant")
        ha.get_context.side_effect = RuntimeError("HA down")
        ha.get_context_for_entity.return_value = {"state": "ok"}
        handlers = {"homeassistant": ha}
        event = _make_event()

        dep_ctx = DependencyContext(
            entity_id="sensor.temperature",
            upstream=[_make_dep_node("sensor.humidity")],
        )

        result = await gather_multi_handler_context(
            event, dep_ctx, handlers, _empty_graph(),
        )

        assert "primary" not in result
        assert "dependency:homeassistant:sensor.humidity" in result
