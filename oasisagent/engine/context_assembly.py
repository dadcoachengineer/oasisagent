"""Multi-handler context assembly for T2 diagnosis.

Gathers diagnostic context from all handlers in the dependency subgraph
before T2 reasoning. The primary handler uses ``get_context(event)`` for
richer diagnostics; dependency nodes use ``get_context_for_entity(entity_id)``
for targeted cross-entity lookups.

Called lazily from the decision engine — only when the pipeline actually
reaches T2 — so handler API calls are skipped for T0 matches and T1 drops.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from oasisagent.engine.service_graph import ServiceGraph
    from oasisagent.handlers.base import Handler
    from oasisagent.models import DependencyContext, DependencyNode, Event

logger = logging.getLogger(__name__)

# Timeout for individual handler get_context_for_entity() calls (seconds).
_HANDLER_TIMEOUT: float = 5.0


def _handler_for_node(
    node: DependencyNode,
    handlers: dict[str, Handler],
    graph: ServiceGraph,
) -> str | None:
    """Map a dependency node to a handler name.

    Resolution order:
    1. TopologyNode.source starts with "auto:" -> handler name after prefix
    2. entity_id contains ":" -> prefix before first ":" is handler name
    3. entity_id contains "." but no ":" -> "homeassistant" (HA entity pattern)
    4. Otherwise -> None (no handler)
    """
    # Look up the topology node to check its source field
    topo_node = graph._nodes.get(node.entity_id)
    if topo_node is not None and topo_node.source.startswith("auto:"):
        handler_name = topo_node.source[5:]
        if handler_name in handlers:
            return handler_name

    # Fall back to entity_id prefix parsing
    if ":" in node.entity_id:
        prefix = node.entity_id.split(":")[0]
        if prefix in handlers:
            return prefix

    # HA entity pattern: "sensor.temperature", "switch.living_room"
    if "." in node.entity_id and ":" not in node.entity_id and "homeassistant" in handlers:
        return "homeassistant"

    return None


async def gather_multi_handler_context(
    event: Event,
    dependency_ctx: DependencyContext | None,
    handlers: dict[str, Handler],
    graph: ServiceGraph,
) -> dict[str, Any]:
    """Gather diagnostic context from multiple handlers.

    Returns a dict keyed by source:
    - ``"primary"``: Full event context from the primary handler
    - ``"dependency:{handler}:{entity_id}"``: Context for each dependency node

    All exceptions are swallowed (logged as warnings) so a single handler
    failure never blocks the T2 pipeline.
    """
    context: dict[str, Any] = {}
    seen: set[tuple[str, str]] = set()  # (handler_name, entity_id) dedup

    # Primary handler: uses get_context(event) for richer diagnostics
    # (has event_type, payload, severity — not just entity_id)
    primary_handler = handlers.get(event.system)
    if primary_handler is not None:
        try:
            primary_ctx = await asyncio.wait_for(
                primary_handler.get_context(event),
                timeout=_HANDLER_TIMEOUT,
            )
            context["primary"] = primary_ctx
        except Exception:
            logger.warning(
                "Primary handler %s.get_context() failed for event %s",
                event.system, event.id,
            )
        seen.add((event.system, event.entity_id))

    if dependency_ctx is None:
        return context

    # Collect dependency nodes to query
    all_dep_nodes: list[DependencyNode] = [
        *dependency_ctx.upstream,
        *dependency_ctx.downstream,
        *dependency_ctx.same_host,
    ]

    async def _fetch_dep_context(
        node: DependencyNode,
    ) -> tuple[str, dict[str, Any]] | None:
        handler_name = _handler_for_node(node, handlers, graph)
        if handler_name is None:
            return None

        # Dedup: check-then-add is safe because single-threaded asyncio has
        # no await between the `in` check and `add()`, so no other coroutine
        # can interleave here even under asyncio.gather().
        dedup_key = (handler_name, node.entity_id)
        if dedup_key in seen:
            return None
        seen.add(dedup_key)

        handler = handlers[handler_name]
        try:
            dep_ctx = await asyncio.wait_for(
                handler.get_context_for_entity(node.entity_id),
                timeout=_HANDLER_TIMEOUT,
            )
            key = f"dependency:{handler_name}:{node.entity_id}"
            return (key, dep_ctx)
        except Exception:
            logger.warning(
                "Handler %s.get_context_for_entity(%s) failed",
                handler_name, node.entity_id,
            )
            return None

    # Run dependency context calls concurrently
    results = await asyncio.gather(
        *[_fetch_dep_context(node) for node in all_dep_nodes],
        return_exceptions=True,
    )

    for result in results:
        if isinstance(result, Exception):
            logger.warning("Unexpected error in dependency context gather: %s", result)
            continue
        if result is not None:
            key, dep_ctx = result
            context[key] = dep_ctx

    return context
