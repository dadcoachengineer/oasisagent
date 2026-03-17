"""Portainer polling ingestion adapter.

Polls the Portainer API for endpoint status, container states,
stack health, and (optionally) container resource usage. Emits events
on state transitions and threshold crossings.

Separate state trackers handle different endpoint semantics:
- Endpoint states: (name, online/offline) — state-based dedup
- Container states: (endpoint/name, effective_state) — state-based dedup
- Stack health: (endpoint/stack, running/total) — transition-based
- Container resources: CPU/memory threshold crossings (opt-in)
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from oasisagent.clients.portainer import PortainerClient
from oasisagent.ingestion.base import IngestAdapter
from oasisagent.models import Event, EventMetadata, Severity, TopologyEdge, TopologyNode

if TYPE_CHECKING:
    from oasisagent.config import PortainerAdapterConfig
    from oasisagent.engine.queue import EventQueue

logger = logging.getLogger(__name__)

# Docker-type endpoint types in Portainer (1=local, 2=agent)
_DOCKER_ENDPOINT_TYPES: frozenset[int] = frozenset({1, 2})

# Container states that are considered alert conditions
_ALERT_STATES: frozenset[str] = frozenset({
    "exited", "dead", "restarting", "created", "unhealthy",
})


class PortainerAdapter(IngestAdapter):
    """Polls Portainer API for container and endpoint events.

    State-based dedup ensures events fire only on state transitions.
    Each poll endpoint has independent error handling.
    """

    def __init__(self, config: PortainerAdapterConfig, queue: EventQueue) -> None:
        super().__init__(queue)
        self._config = config
        self._client = PortainerClient(
            url=config.url,
            api_key=config.api_key,
            verify_ssl=config.verify_ssl,
            timeout=config.timeout,
        )
        self._stopping = False
        self._task: asyncio.Task[None] | None = None

        # Per-endpoint health tracking
        self._endpoint_health: dict[str, bool] = {}
        if config.poll_endpoints:
            self._endpoint_health["endpoints"] = False
        if config.poll_containers:
            self._endpoint_health["containers"] = False
        if config.poll_container_resources:
            self._endpoint_health["resources"] = False

        # Dedup trackers
        self._endpoint_states: dict[str, str] = {}       # name → "online"|"offline"
        self._known_endpoints: dict[str, int] = {}        # name → endpoint_id
        self._endpoint_meta: dict[str, dict[str, str]] = {}  # name → {PublicURL, URL}
        self._container_states: dict[str, str] = {}       # "ep/name" → effective_state
        self._container_ids: dict[str, str] = {}          # "ep/name" → Docker container ID
        self._stack_health: dict[str, tuple[int, int]] = {}  # "ep/stack" → (running, total)
        self._container_cpu_alert: dict[str, bool] = {}
        self._container_mem_alert: dict[str, bool] = {}

        # Round-robin rotation for resource polling fairness
        self._resource_poll_order: deque[str] = deque()
        self._last_polled_count: int = 0

    @property
    def name(self) -> str:
        return "portainer"

    async def start(self) -> None:
        """Connect to Portainer and start the polling loop."""
        try:
            await self._client.start()
        except Exception as exc:
            logger.error("Portainer adapter: connection failed: %s", exc)
            for key in self._endpoint_health:
                self._endpoint_health[key] = False
            return

        self._task = asyncio.create_task(
            self._poll_loop(), name="portainer-poller",
        )
        await self._task

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
        await self._client.close()
        for key in self._endpoint_health:
            self._endpoint_health[key] = False

    async def healthy(self) -> bool:
        if not self._endpoint_health:
            return False
        return any(self._endpoint_health.values())

    def health_detail(self) -> dict[str, str]:
        """Per-endpoint health for dashboard display."""
        return {
            endpoint: ("connected" if ok else "disconnected")
            for endpoint, ok in self._endpoint_health.items()
        }

    # -----------------------------------------------------------------
    # Poll loop
    # -----------------------------------------------------------------

    async def _poll_loop(self) -> None:
        """Main polling loop — sequential ordering.

        Endpoints first (seeds _known_endpoints), then containers
        (uses _known_endpoints), then resources (opt-in, expensive).
        """
        while not self._stopping:
            if self._config.poll_endpoints:
                try:
                    await self._poll_endpoints()
                    self._endpoint_health["endpoints"] = True
                except asyncio.CancelledError:
                    return
                except Exception as exc:
                    logger.warning("Portainer endpoints poll error: %s", exc)
                    self._endpoint_health["endpoints"] = False

            if self._config.poll_containers:
                try:
                    await self._poll_containers()
                    self._endpoint_health["containers"] = True
                except asyncio.CancelledError:
                    return
                except Exception as exc:
                    logger.warning("Portainer containers poll error: %s", exc)
                    self._endpoint_health["containers"] = False

            if self._config.poll_container_resources:
                try:
                    await self._poll_container_resources()
                    self._endpoint_health["resources"] = True
                except asyncio.CancelledError:
                    return
                except Exception as exc:
                    logger.warning("Portainer resources poll error: %s", exc)
                    self._endpoint_health["resources"] = False

            # Interruptible sleep
            for _ in range(self._config.poll_interval):
                if self._stopping:
                    return
                await asyncio.sleep(1)

    # -----------------------------------------------------------------
    # Endpoint polling
    # -----------------------------------------------------------------

    async def _poll_endpoints(self) -> None:
        """Poll /api/endpoints for Docker endpoint online/offline transitions."""
        data = await self._client.get("/api/endpoints")
        if not isinstance(data, list):
            return

        for ep in data:
            ep_type = ep.get("Type", 0)
            if ep_type not in _DOCKER_ENDPOINT_TYPES:
                continue

            ep_name = ep.get("Name", "")
            if not ep_name:
                continue

            ep_id = ep.get("Id")
            if ep_id is None:
                continue

            # Seed known endpoints for downstream polls
            self._known_endpoints[ep_name] = ep_id
            self._endpoint_meta[ep_name] = {
                "PublicURL": ep.get("PublicURL", ""),
                "URL": ep.get("URL", ""),
            }

            status_val = ep.get("Status", 0)
            status = "online" if status_val == 1 else "offline"

            prev = self._endpoint_states.get(ep_name)
            self._endpoint_states[ep_name] = status

            # First poll — seed only
            if prev is None:
                continue

            if prev != status:
                if status == "offline":
                    self._enqueue(Event(
                        source=self.name,
                        system="portainer",
                        event_type="ptr_endpoint_unreachable",
                        entity_id=f"portainer:{ep_name}",
                        severity=Severity.ERROR,
                        timestamp=datetime.now(tz=UTC),
                        payload={
                            "endpoint_name": ep_name,
                            "endpoint_id": ep_id,
                            "status": status_val,
                        },
                        metadata=EventMetadata(
                            dedup_key=f"portainer:endpoint:{ep_name}:state",
                        ),
                    ))
                else:
                    self._enqueue(Event(
                        source=self.name,
                        system="portainer",
                        event_type="ptr_endpoint_recovered",
                        entity_id=f"portainer:{ep_name}",
                        severity=Severity.INFO,
                        timestamp=datetime.now(tz=UTC),
                        payload={
                            "endpoint_name": ep_name,
                            "endpoint_id": ep_id,
                            "status": status_val,
                        },
                        metadata=EventMetadata(
                            dedup_key=f"portainer:endpoint:{ep_name}:state",
                        ),
                    ))

    # -----------------------------------------------------------------
    # Container polling
    # -----------------------------------------------------------------

    async def _poll_containers(self) -> None:
        """Poll containers per online endpoint, emit state transition events."""
        current_keys: set[str] = set()
        current_stacks: set[str] = set()

        for ep_name, ep_id in list(self._known_endpoints.items()):
            # Skip offline endpoints
            if self._endpoint_states.get(ep_name) == "offline":
                continue

            try:
                containers = await self._client.get_docker(
                    ep_id, "containers/json", all="true",
                )
            except Exception as exc:
                logger.debug(
                    "Portainer container poll for endpoint %s failed: %s",
                    ep_name, exc,
                )
                continue

            if not isinstance(containers, list):
                continue

            # Stack tracking: group by compose project label
            stack_counts: dict[str, tuple[int, int]] = {}  # stack → (running, total)

            for ct in containers:
                raw_names = ct.get("Names", [])
                ct_name = raw_names[0].lstrip("/") if raw_names else ""
                if not ct_name:
                    continue

                # Skip ignored containers
                if ct_name in self._config.ignore_containers:
                    continue

                ct_id = ct.get("Id", "")
                state = ct.get("State", "").lower()
                status_text = ct.get("Status", "")
                image = ct.get("Image", "")
                labels = ct.get("Labels", {}) or {}
                stack = labels.get("com.docker.compose.project", "")

                # Compute effective state
                effective = self._compute_effective_state(state, status_text)

                key = f"{ep_name}/{ct_name}"
                current_keys.add(key)

                # Cache container ID for resource polling
                if ct_id:
                    self._container_ids[key] = ct_id

                prev = self._container_states.get(key)
                self._container_states[key] = effective

                # First poll — seed only
                if prev is not None and prev != effective:
                    self._emit_container_event(
                        effective, prev, key, ep_name, ep_id,
                        ct_name, ct_id, image, stack,
                    )

                # Stack counting
                if stack:
                    stack_key = f"{ep_name}/{stack}"
                    current_stacks.add(stack_key)
                    running, total = stack_counts.get(stack, (0, 0))
                    total += 1
                    if state == "running":
                        running += 1
                    stack_counts[stack] = (running, total)

            # Stack events
            if self._config.poll_stacks:
                for stack, (running, total) in stack_counts.items():
                    stack_key = f"{ep_name}/{stack}"
                    self._emit_stack_event(
                        stack_key, stack, ep_name, ep_id, running, total,
                    )

        # Stale cleanup: remove containers no longer present
        stale = set(self._container_states.keys()) - current_keys
        for key in stale:
            del self._container_states[key]
            self._container_ids.pop(key, None)
            self._container_cpu_alert.pop(key, None)
            self._container_mem_alert.pop(key, None)

        # Stale stack cleanup
        stale_stacks = set(self._stack_health.keys()) - current_stacks
        for key in stale_stacks:
            del self._stack_health[key]

    @staticmethod
    def _compute_effective_state(state: str, status_text: str) -> str:
        """Compute effective container state from state + status text."""
        if "(unhealthy)" in status_text.lower():
            return "unhealthy"
        if state in _ALERT_STATES:
            return state
        return "ok"

    def _emit_container_event(
        self,
        effective: str,
        prev: str,
        key: str,
        ep_name: str,
        ep_id: int,
        ct_name: str,
        ct_id: str,
        image: str,
        stack: str,
    ) -> None:
        """Emit a container state transition event."""
        # Map effective state to event type and severity
        state_map: dict[str, tuple[str, Severity]] = {
            "exited": ("ptr_container_exited", Severity.ERROR),
            "dead": ("ptr_container_dead", Severity.ERROR),
            "unhealthy": ("ptr_container_unhealthy", Severity.WARNING),
            "restarting": ("ptr_container_restarting", Severity.WARNING),
            "created": ("ptr_container_created", Severity.WARNING),
        }

        if effective in state_map:
            event_type, severity = state_map[effective]
        elif effective == "ok" and prev in _ALERT_STATES:
            event_type = "ptr_container_recovered"
            severity = Severity.INFO
        else:
            return

        payload: dict[str, object] = {
            "endpoint_name": ep_name,
            "endpoint_id": ep_id,
            "container_id": ct_id,
            "container_name": ct_name,
            "image": image,
            "state": effective,
            "previous_state": prev,
        }
        if stack:
            payload["stack"] = stack

        self._enqueue(Event(
            source=self.name,
            system="portainer",
            event_type=event_type,
            entity_id=f"portainer:{key}",
            severity=severity,
            timestamp=datetime.now(tz=UTC),
            payload=payload,
            metadata=EventMetadata(
                dedup_key=f"portainer:container:{key}:state",
            ),
        ))

    def _emit_stack_event(
        self,
        stack_key: str,
        stack_name: str,
        ep_name: str,
        ep_id: int,
        running: int,
        total: int,
    ) -> None:
        """Emit stack health transition events."""
        prev = self._stack_health.get(stack_key)
        self._stack_health[stack_key] = (running, total)

        # First poll — seed only
        if prev is None:
            return

        prev_running, prev_total = prev

        # Determine current and previous health status
        def _health(r: int, t: int) -> str:
            if t == 0:
                return "ok"
            if r == t:
                return "ok"
            if r == 0:
                return "down"
            return "degraded"

        cur_health = _health(running, total)
        prev_health = _health(prev_running, prev_total)

        if cur_health == prev_health:
            return

        event_map: dict[str, tuple[str, Severity]] = {
            "degraded": ("ptr_stack_degraded", Severity.WARNING),
            "down": ("ptr_stack_down", Severity.ERROR),
            "ok": ("ptr_stack_recovered", Severity.INFO),
        }

        if cur_health not in event_map:
            return

        event_type, severity = event_map[cur_health]
        self._enqueue(Event(
            source=self.name,
            system="portainer",
            event_type=event_type,
            entity_id=f"portainer:{stack_key}",
            severity=severity,
            timestamp=datetime.now(tz=UTC),
            payload={
                "endpoint_name": ep_name,
                "endpoint_id": ep_id,
                "stack": stack_name,
                "running": running,
                "total": total,
            },
            metadata=EventMetadata(
                dedup_key=f"portainer:stack:{stack_key}:health",
            ),
        ))

    # -----------------------------------------------------------------
    # Container resource polling (opt-in)
    # -----------------------------------------------------------------

    async def _poll_container_resources(self) -> None:
        """Poll per-container stats for CPU/memory threshold crossings.

        Uses bounded concurrency (semaphore) to poll multiple containers
        in parallel, with round-robin rotation (deque.rotate) so all
        containers get equal polling frequency over N cycles if the time
        budget is still exhausted.
        """
        deadline = time.monotonic() + self._config.poll_interval * 0.8

        # Build ordered list of pollable (key, ep_id) pairs
        eligible: list[tuple[str, int]] = []
        for ep_name, ep_id in list(self._known_endpoints.items()):
            if self._endpoint_states.get(ep_name) == "offline":
                continue
            for key, effective in list(self._container_states.items()):
                if not key.startswith(f"{ep_name}/"):
                    continue
                if effective != "ok":
                    continue
                if self._container_ids.get(key):
                    eligible.append((key, ep_id))

        # Sync deque with current eligible set, preserving rotation order
        eligible_keys = {k for k, _ in eligible}
        ep_lookup = {k: eid for k, eid in eligible}

        # Remove stale entries
        self._resource_poll_order = deque(
            k for k in self._resource_poll_order if k in eligible_keys
        )
        # Add new entries at the end
        for key in eligible_keys - set(self._resource_poll_order):
            self._resource_poll_order.append(key)

        # Rotate by how many we polled last cycle for fairness
        if self._last_polled_count > 0:
            self._resource_poll_order.rotate(-self._last_polled_count)

        sem = asyncio.Semaphore(self._config.resource_poll_concurrency)
        results: dict[str, dict[str, object]] = {}
        budget_exceeded = False

        async def _fetch_one(key: str, ct_id: str, ep_id: int) -> None:
            nonlocal budget_exceeded
            if budget_exceeded:
                return
            async with sem:
                if time.monotonic() > deadline:
                    budget_exceeded = True
                    return
                try:
                    stats = await self._client.get_docker(
                        ep_id, f"containers/{ct_id}/stats",
                        stream="false",
                    )
                    if isinstance(stats, dict):
                        results[key] = stats
                except Exception:
                    pass

        tasks: list[asyncio.Task[None]] = []
        for key in list(self._resource_poll_order):
            ct_id = self._container_ids.get(key)
            ep_id = ep_lookup.get(key)
            if ct_id and ep_id is not None:
                tasks.append(asyncio.create_task(_fetch_one(key, ct_id, ep_id)))

        if tasks:
            await asyncio.gather(*tasks)

        if budget_exceeded:
            logger.warning(
                "Portainer resource poll exceeded time budget after "
                "%d/%d containers",
                len(results),
                len(self._resource_poll_order),
            )

        for key, stats in results.items():
            self._check_container_resources(key, stats)

        self._last_polled_count = len(results)

    def _check_container_resources(
        self, key: str, stats: dict[str, object],
    ) -> None:
        """Check CPU and memory thresholds for a single container."""
        # CPU: delta-based calculation
        cpu_stats = stats.get("cpu_stats", {})
        precpu_stats = stats.get("precpu_stats", {})
        if isinstance(cpu_stats, dict) and isinstance(precpu_stats, dict):
            cpu_usage = cpu_stats.get("cpu_usage", {})
            precpu_usage = precpu_stats.get("cpu_usage", {})
            if isinstance(cpu_usage, dict) and isinstance(precpu_usage, dict):
                cpu_delta = (
                    (cpu_usage.get("total_usage", 0) or 0)
                    - (precpu_usage.get("total_usage", 0) or 0)
                )
                system_delta = (
                    (cpu_stats.get("system_cpu_usage", 0) or 0)
                    - (precpu_stats.get("system_cpu_usage", 0) or 0)
                )
                online_cpus = cpu_stats.get("online_cpus", 1) or 1

                if system_delta > 0 and cpu_delta >= 0:
                    cpu_pct = (cpu_delta / system_delta) * online_cpus * 100
                    self._check_threshold(
                        key, "cpu", cpu_pct,
                        self._config.cpu_threshold,
                        self._container_cpu_alert,
                        "ptr_container_high_cpu",
                        "ptr_container_cpu_recovered",
                    )

        # Memory: usage / limit
        mem_stats = stats.get("memory_stats", {})
        if isinstance(mem_stats, dict):
            usage = mem_stats.get("usage", 0) or 0
            limit = mem_stats.get("limit", 0) or 0
            if limit > 0:
                mem_pct = usage / limit * 100
                self._check_threshold(
                    key, "memory", mem_pct,
                    self._config.memory_threshold,
                    self._container_mem_alert,
                    "ptr_container_high_memory",
                    "ptr_container_memory_recovered",
                )

    def _check_threshold(
        self,
        entity: str,
        resource: str,
        value: float,
        threshold: float,
        alert_tracker: dict[str, bool],
        alert_event: str,
        recovery_event: str,
    ) -> None:
        """Emit events on resource threshold transitions."""
        was_alert = alert_tracker.get(entity, False)
        is_alert = value >= threshold
        alert_tracker[entity] = is_alert

        if is_alert and not was_alert:
            self._enqueue(Event(
                source=self.name,
                system="portainer",
                event_type=alert_event,
                entity_id=f"portainer:{entity}",
                severity=Severity.WARNING,
                timestamp=datetime.now(tz=UTC),
                payload={
                    "container": entity,
                    resource: round(value, 1),
                    "threshold": threshold,
                },
                metadata=EventMetadata(
                    dedup_key=f"portainer:container:{entity}:{resource}",
                ),
            ))
        elif not is_alert and was_alert:
            self._enqueue(Event(
                source=self.name,
                system="portainer",
                event_type=recovery_event,
                entity_id=f"portainer:{entity}",
                severity=Severity.INFO,
                timestamp=datetime.now(tz=UTC),
                payload={
                    "container": entity,
                    resource: round(value, 1),
                    "threshold": threshold,
                },
                metadata=EventMetadata(
                    dedup_key=f"portainer:container:{entity}:{resource}",
                ),
            ))

    # -----------------------------------------------------------------
    # Topology discovery
    # -----------------------------------------------------------------

    async def discover_topology(
        self,
    ) -> tuple[list[TopologyNode], list[TopologyEdge]]:
        """Discover Docker hosts, containers, and stack relationships.

        Creates:
        - Host nodes for each Portainer endpoint
        - Container nodes with ``runs_on`` edges to their host
        - Stack nodes with ``member_of`` edges from containers
        """
        nodes: list[TopologyNode] = []
        edges: list[TopologyEdge] = []
        source = f"auto:{self.name}"
        now = datetime.now(UTC)

        try:
            endpoints = await self._client.get("/api/endpoints")
        except Exception:
            logger.debug("Portainer topology discovery failed (API unreachable)")
            return [], []

        if not isinstance(endpoints, list):
            return [], []

        for ep in endpoints:
            ep_type = ep.get("Type", 0)
            if ep_type not in _DOCKER_ENDPOINT_TYPES:
                continue

            ep_name = ep.get("Name", "")
            ep_id = ep.get("Id")
            if not ep_name or ep_id is None:
                continue

            # Host node (Docker endpoint)
            public_url = ep.get("PublicURL", "")
            host_ip = public_url.split("://")[-1].split(":")[0] if public_url else ""

            nodes.append(TopologyNode(
                entity_id=f"portainer:{ep_name}",
                entity_type="host",
                display_name=ep_name,
                host_ip=host_ip,
                source=source,
                last_seen=now,
                metadata={
                    "endpoint_id": ep_id,
                    "status": ep.get("Status", 0),
                    "url": ep.get("URL", ""),
                },
            ))

            # Containers on this endpoint
            try:
                containers = await self._client.get_docker(
                    ep_id, "containers/json", all="true",
                )
            except Exception:
                continue

            if not isinstance(containers, list):
                continue

            stacks_seen: set[str] = set()

            for ct in containers:
                raw_names = ct.get("Names", [])
                ct_name = raw_names[0].lstrip("/") if raw_names else ""
                if not ct_name:
                    continue

                labels = ct.get("Labels", {}) or {}
                stack = labels.get("com.docker.compose.project", "")

                entity_id = f"portainer:{ep_name}/{ct_name}"
                meta: dict[str, object] = {
                    "state": ct.get("State", ""),
                    "image": ct.get("Image", ""),
                }
                if stack:
                    meta["stack"] = stack

                nodes.append(TopologyNode(
                    entity_id=entity_id,
                    entity_type="container",
                    display_name=ct_name,
                    source=source,
                    last_seen=now,
                    metadata=meta,
                ))

                edges.append(TopologyEdge(
                    from_entity=entity_id,
                    to_entity=f"portainer:{ep_name}",
                    edge_type="runs_on",
                    source=source,
                    last_seen=now,
                ))

                # Stack grouping
                if stack:
                    if stack not in stacks_seen:
                        stacks_seen.add(stack)
                        nodes.append(TopologyNode(
                            entity_id=f"portainer:{ep_name}/stack:{stack}",
                            entity_type="stack",
                            display_name=stack,
                            source=source,
                            last_seen=now,
                            metadata={"endpoint": ep_name},
                        ))
                        edges.append(TopologyEdge(
                            from_entity=f"portainer:{ep_name}/stack:{stack}",
                            to_entity=f"portainer:{ep_name}",
                            edge_type="runs_on",
                            source=source,
                            last_seen=now,
                        ))

                    edges.append(TopologyEdge(
                        from_entity=entity_id,
                        to_entity=f"portainer:{ep_name}/stack:{stack}",
                        edge_type="member_of",
                        source=source,
                        last_seen=now,
                    ))

        return nodes, edges
