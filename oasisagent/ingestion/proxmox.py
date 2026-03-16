"""Proxmox VE polling ingestion adapter.

Polls the Proxmox cluster API for node status, VM/CT states, node
resource usage, replication jobs, and task failures. Emits events on
state transitions, threshold crossings, or failed tasks.

Separate state trackers handle different endpoint semantics:
- Node states: (name, online/offline) — state-based dedup
- VM states: (entity_id, status) — state-based dedup
- Node resources: CPU/memory threshold crossings
- Replication: fail_count-based state transitions
- Tasks: time-based lookback + UPID dedup
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from oasisagent.clients.proxmox import ProxmoxClient
from oasisagent.ingestion.base import IngestAdapter
from oasisagent.models import Event, EventMetadata, Severity, TopologyEdge, TopologyNode

if TYPE_CHECKING:
    from oasisagent.config import ProxmoxAdapterConfig
    from oasisagent.engine.queue import EventQueue

logger = logging.getLogger(__name__)


class ProxmoxAdapter(IngestAdapter):
    """Polls Proxmox VE cluster API for infrastructure events.

    State-based dedup ensures events fire only on state transitions.
    Each endpoint has independent error handling so one failing endpoint
    doesn't block the others.
    """

    def __init__(self, config: ProxmoxAdapterConfig, queue: EventQueue) -> None:
        super().__init__(queue)
        self._config = config
        self._client = ProxmoxClient(
            url=config.url,
            user=config.user,
            token_name=config.token_name,
            token_value=config.token_value,
            verify_ssl=config.verify_ssl,
            timeout=config.timeout,
        )
        self._stopping = False
        self._task: asyncio.Task[None] | None = None

        # Per-endpoint health tracking
        self._endpoint_health: dict[str, bool] = {}
        if config.poll_nodes:
            self._endpoint_health["nodes"] = False
            self._endpoint_health["node_resources"] = False
        if config.poll_vms:
            self._endpoint_health["vms"] = False
        if config.poll_replication:
            self._endpoint_health["replication"] = False
        if config.poll_tasks:
            self._endpoint_health["tasks"] = False

        # Dedup trackers
        self._node_states: dict[str, str] = {}          # node_name → "online"|"offline"
        self._vm_states: dict[str, str] = {}             # entity_id → status
        self._node_cpu_alert: dict[str, bool] = {}       # node_name → alert active
        self._node_mem_alert: dict[str, bool] = {}       # node_name → alert active
        self._repl_states: dict[str, bool] = {}          # pve:{vmid} → failing

        # Task dedup
        self._last_task_poll: datetime | None = None
        self._seen_tasks: dict[str, float] = {}          # UPID → monotonic timestamp

    @property
    def name(self) -> str:
        return "proxmox"

    async def start(self) -> None:
        """Connect to PVE and start the polling loop."""
        try:
            await self._client.start()
        except Exception as exc:
            logger.error("Proxmox adapter: connection failed: %s", exc)
            for key in self._endpoint_health:
                self._endpoint_health[key] = False
            return

        self._task = asyncio.create_task(
            self._poll_loop(), name="proxmox-poller",
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
        """Main polling loop — polls endpoints sequentially each interval.

        Sequential ordering guarantees _poll_nodes() populates
        _node_states before _poll_node_resources() needs it.
        """
        while not self._stopping:
            # Nodes first — seeds _node_states for node_resources
            if self._config.poll_nodes:
                try:
                    await self._poll_nodes()
                    self._endpoint_health["nodes"] = True
                except asyncio.CancelledError:
                    return
                except Exception as exc:
                    logger.warning("Proxmox nodes poll error: %s", exc)
                    self._endpoint_health["nodes"] = False

                try:
                    await self._poll_node_resources()
                    self._endpoint_health["node_resources"] = True
                except asyncio.CancelledError:
                    return
                except Exception as exc:
                    logger.warning("Proxmox node resources poll error: %s", exc)
                    self._endpoint_health["node_resources"] = False

            if self._config.poll_vms:
                try:
                    await self._poll_vms()
                    self._endpoint_health["vms"] = True
                except asyncio.CancelledError:
                    return
                except Exception as exc:
                    logger.warning("Proxmox VMs poll error: %s", exc)
                    self._endpoint_health["vms"] = False

            if self._config.poll_replication:
                try:
                    await self._poll_replication()
                    self._endpoint_health["replication"] = True
                except asyncio.CancelledError:
                    return
                except Exception as exc:
                    logger.warning("Proxmox replication poll error: %s", exc)
                    self._endpoint_health["replication"] = False

            if self._config.poll_tasks:
                try:
                    await self._poll_tasks()
                    self._endpoint_health["tasks"] = True
                except asyncio.CancelledError:
                    return
                except Exception as exc:
                    logger.warning("Proxmox tasks poll error: %s", exc)
                    self._endpoint_health["tasks"] = False

            # Interruptible sleep
            for _ in range(self._config.poll_interval):
                if self._stopping:
                    return
                await asyncio.sleep(1)

    # -----------------------------------------------------------------
    # Node polling
    # -----------------------------------------------------------------

    async def _poll_nodes(self) -> None:
        """Poll /cluster/status for node online/offline transitions."""
        data = await self._client.get("/api2/json/cluster/status")
        if not isinstance(data, list):
            return

        for item in data:
            if item.get("type") != "node":
                continue

            node_name = item.get("name", "")
            if not node_name:
                continue

            online = item.get("online", 0)
            status = "online" if online == 1 else "offline"

            prev = self._node_states.get(node_name)
            self._node_states[node_name] = status

            # First poll — seed only, no events
            if prev is None:
                continue

            if prev != status:
                if status == "offline":
                    self._enqueue(Event(
                        source=self.name,
                        system="proxmox",
                        event_type="pve_node_unreachable",
                        entity_id=node_name,
                        severity=Severity.ERROR,
                        timestamp=datetime.now(tz=UTC),
                        payload={"node": node_name, "online": online},
                        metadata=EventMetadata(
                            dedup_key=f"proxmox:node:{node_name}:state",
                        ),
                    ))
                else:
                    self._enqueue(Event(
                        source=self.name,
                        system="proxmox",
                        event_type="pve_node_recovered",
                        entity_id=node_name,
                        severity=Severity.INFO,
                        timestamp=datetime.now(tz=UTC),
                        payload={"node": node_name, "online": online},
                        metadata=EventMetadata(
                            dedup_key=f"proxmox:node:{node_name}:state",
                        ),
                    ))

    # -----------------------------------------------------------------
    # Node resource polling
    # -----------------------------------------------------------------

    async def _poll_node_resources(self) -> None:
        """Poll /nodes/{node}/status for CPU/memory threshold crossings."""
        for node_name in list(self._node_states.keys()):
            try:
                data = await self._client.get(
                    f"/api2/json/nodes/{node_name}/status",
                )
            except Exception as exc:
                logger.debug("Proxmox node %s status poll failed: %s", node_name, exc)
                continue

            if not isinstance(data, dict):
                continue

            # CPU: data["cpu"] is 0.0-1.0 fraction
            cpu_frac = data.get("cpu")
            if cpu_frac is not None:
                try:
                    cpu_pct = float(cpu_frac) * 100
                except (TypeError, ValueError):
                    cpu_pct = None

                if cpu_pct is not None:
                    self._check_threshold(
                        node_name, "cpu", cpu_pct,
                        self._config.cpu_threshold,
                        self._node_cpu_alert,
                        "pve_node_high_cpu", "pve_node_cpu_recovered",
                    )

            # Memory: data["memory"]["used"] / data["memory"]["total"]
            mem = data.get("memory", {})
            if isinstance(mem, dict):
                used = mem.get("used", 0)
                total = mem.get("total", 0)
                if total > 0:
                    mem_pct = used / total * 100
                    self._check_threshold(
                        node_name, "memory", mem_pct,
                        self._config.memory_threshold,
                        self._node_mem_alert,
                        "pve_node_high_memory", "pve_node_memory_recovered",
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
                system="proxmox",
                event_type=alert_event,
                entity_id=entity,
                severity=Severity.WARNING,
                timestamp=datetime.now(tz=UTC),
                payload={
                    "node": entity,
                    resource: round(value, 1),
                    "threshold": threshold,
                },
                metadata=EventMetadata(
                    dedup_key=f"proxmox:node:{entity}:{resource}",
                ),
            ))
        elif not is_alert and was_alert:
            self._enqueue(Event(
                source=self.name,
                system="proxmox",
                event_type=recovery_event,
                entity_id=entity,
                severity=Severity.INFO,
                timestamp=datetime.now(tz=UTC),
                payload={
                    "node": entity,
                    resource: round(value, 1),
                    "threshold": threshold,
                },
                metadata=EventMetadata(
                    dedup_key=f"proxmox:node:{entity}:{resource}",
                ),
            ))

    # -----------------------------------------------------------------
    # VM/CT polling
    # -----------------------------------------------------------------

    async def _poll_vms(self) -> None:
        """Poll /cluster/resources?type=vm for VM/CT state transitions."""
        data = await self._client.get(
            "/api2/json/cluster/resources", type="vm",
        )
        if not isinstance(data, list):
            return

        for vm in data:
            vmid = vm.get("vmid")
            if vmid is None:
                continue

            entity_id = f"pve:{vmid}"
            status = vm.get("status", "unknown")
            vm_name = vm.get("name", str(vmid))

            prev = self._vm_states.get(entity_id)
            self._vm_states[entity_id] = status

            # First poll — seed only
            if prev is None:
                continue

            if prev != status:
                if status == "stopped":
                    self._enqueue(Event(
                        source=self.name,
                        system="proxmox",
                        event_type="pve_vm_stopped",
                        entity_id=entity_id,
                        severity=Severity.WARNING,
                        timestamp=datetime.now(tz=UTC),
                        payload={
                            "vmid": vmid,
                            "name": vm_name,
                            "status": status,
                            "previous_status": prev,
                            "node": vm.get("node", ""),
                        },
                        metadata=EventMetadata(
                            dedup_key=f"proxmox:vm:{entity_id}:state",
                        ),
                    ))
                elif status == "running":
                    self._enqueue(Event(
                        source=self.name,
                        system="proxmox",
                        event_type="pve_vm_running",
                        entity_id=entity_id,
                        severity=Severity.INFO,
                        timestamp=datetime.now(tz=UTC),
                        payload={
                            "vmid": vmid,
                            "name": vm_name,
                            "status": status,
                            "previous_status": prev,
                            "node": vm.get("node", ""),
                        },
                        metadata=EventMetadata(
                            dedup_key=f"proxmox:vm:{entity_id}:state",
                        ),
                    ))
                elif status == "locked":
                    self._enqueue(Event(
                        source=self.name,
                        system="proxmox",
                        event_type="pve_vm_locked",
                        entity_id=entity_id,
                        severity=Severity.WARNING,
                        timestamp=datetime.now(tz=UTC),
                        payload={
                            "vmid": vmid,
                            "name": vm_name,
                            "status": status,
                            "previous_status": prev,
                            "node": vm.get("node", ""),
                        },
                        metadata=EventMetadata(
                            dedup_key=f"proxmox:vm:{entity_id}:state",
                        ),
                    ))

    # -----------------------------------------------------------------
    # Replication polling
    # -----------------------------------------------------------------

    async def _poll_replication(self) -> None:
        """Poll /cluster/replication for replication job failures."""
        data = await self._client.get("/api2/json/cluster/replication")
        if not isinstance(data, list):
            return

        for job in data:
            vmid = job.get("guest")
            if vmid is None:
                continue

            entity_id = f"pve:{vmid}"
            fail_count = job.get("fail_count", 0)
            is_failing = fail_count > 0

            prev_failing = self._repl_states.get(entity_id)
            self._repl_states[entity_id] = is_failing

            # First poll — seed only
            if prev_failing is None:
                continue

            if is_failing and not prev_failing:
                self._enqueue(Event(
                    source=self.name,
                    system="proxmox",
                    event_type="pve_replication_failed",
                    entity_id=entity_id,
                    severity=Severity.WARNING,
                    timestamp=datetime.now(tz=UTC),
                    payload={
                        "vmid": vmid,
                        "fail_count": fail_count,
                        "last_sync": job.get("last_sync"),
                        "last_try": job.get("last_try"),
                        "source": job.get("source", ""),
                        "target": job.get("target", ""),
                    },
                    metadata=EventMetadata(
                        dedup_key=f"proxmox:replication:{entity_id}",
                    ),
                ))
            elif not is_failing and prev_failing:
                self._enqueue(Event(
                    source=self.name,
                    system="proxmox",
                    event_type="pve_replication_recovered",
                    entity_id=entity_id,
                    severity=Severity.INFO,
                    timestamp=datetime.now(tz=UTC),
                    payload={
                        "vmid": vmid,
                        "fail_count": fail_count,
                        "last_sync": job.get("last_sync"),
                        "source": job.get("source", ""),
                        "target": job.get("target", ""),
                    },
                    metadata=EventMetadata(
                        dedup_key=f"proxmox:replication:{entity_id}",
                    ),
                ))

    # -----------------------------------------------------------------
    # Task polling
    # -----------------------------------------------------------------

    async def _poll_tasks(self) -> None:
        """Poll /cluster/tasks for failed tasks (backup, migration, etc.)."""
        now = datetime.now(tz=UTC)

        # Evict stale UPID entries
        cutoff = time.monotonic() - 2 * self._config.poll_interval
        self._seen_tasks = {
            upid: ts for upid, ts in self._seen_tasks.items() if ts > cutoff
        }

        if self._last_task_poll is not None:
            since = self._last_task_poll
        else:
            # First poll — look back one poll interval
            since = datetime.fromtimestamp(
                now.timestamp() - self._config.poll_interval, tz=UTC,
            )
        self._last_task_poll = now

        since_epoch = int(since.timestamp())

        data = await self._client.get("/api2/json/cluster/tasks")
        if not isinstance(data, list):
            return

        for task in data:
            upid = task.get("upid", "")
            if not upid:
                continue

            # Skip already-seen tasks
            if upid in self._seen_tasks:
                continue

            # Only look at finished tasks since our lookback
            endtime = task.get("endtime", 0)
            if not endtime or endtime < since_epoch:
                continue

            # Skip successful and still-running tasks
            status = task.get("status", "")
            if status in ("OK", ""):
                continue

            self._seen_tasks[upid] = time.monotonic()

            # Parse UPID to extract target VMID
            # Format: UPID:node:pid:pstart:starttime:type:id:user:
            entity_id = self._entity_from_upid(upid)
            task_type = task.get("type", "")

            event_type = "pve_backup_failed" if task_type == "vzdump" else "pve_task_failed"

            self._enqueue(Event(
                source=self.name,
                system="proxmox",
                event_type=event_type,
                entity_id=entity_id,
                severity=Severity.WARNING,
                timestamp=datetime.now(tz=UTC),
                payload={
                    "upid": upid,
                    "type": task_type,
                    "status": status,
                    "node": task.get("node", ""),
                    "user": task.get("user", ""),
                    "starttime": task.get("starttime"),
                    "endtime": endtime,
                },
                metadata=EventMetadata(
                    dedup_key=f"proxmox:task:{upid}",
                ),
            ))

    @staticmethod
    def _entity_from_upid(upid: str) -> str:
        """Extract entity_id from a Proxmox UPID string.

        UPID format: ``UPID:node:pid:pstart:starttime:type:id:user:``
        Field index 6 is the target ID (VMID for VM operations).
        Falls back to full UPID for node-level tasks without a target VM.
        """
        parts = upid.split(":")
        if len(parts) >= 7:
            target_id = parts[6]
            if target_id and target_id.isdigit():
                return f"pve:{target_id}"
        return upid

    # -----------------------------------------------------------------
    # Topology discovery
    # -----------------------------------------------------------------

    async def discover_topology(
        self,
    ) -> tuple[list[TopologyNode], list[TopologyEdge]]:
        """Discover PVE nodes, VMs, and CTs with runs_on relationships."""
        nodes: list[TopologyNode] = []
        edges: list[TopologyEdge] = []
        source = f"auto:{self.name}"
        now = datetime.now(UTC)

        try:
            cluster_items = await self._client.get(
                "/api2/json/cluster/resources",
            )
        except Exception:
            logger.debug("Proxmox topology discovery failed (API unreachable)")
            return [], []

        if not isinstance(cluster_items, list):
            return [], []

        for item in cluster_items:
            item_type = item.get("type", "")

            if item_type == "node":
                node_name = item.get("node", "")
                if not node_name:
                    continue
                nodes.append(TopologyNode(
                    entity_id=f"proxmox:{node_name}",
                    entity_type="host",
                    display_name=node_name,
                    host_ip=item.get("ip", ""),
                    source=source,
                    last_seen=now,
                    metadata={
                        "status": item.get("status", ""),
                        "maxcpu": item.get("maxcpu", 0),
                        "maxmem": item.get("maxmem", 0),
                    },
                ))

            elif item_type in ("qemu", "lxc"):
                vmid = item.get("vmid")
                if vmid is None:
                    continue
                vm_name = item.get("name", str(vmid))
                host_node = item.get("node", "")

                entity_type = "vm" if item_type == "qemu" else "ct"
                entity_id = f"proxmox:{vmid}"

                nodes.append(TopologyNode(
                    entity_id=entity_id,
                    entity_type=entity_type,
                    display_name=vm_name,
                    source=source,
                    last_seen=now,
                    metadata={
                        "vmid": vmid,
                        "status": item.get("status", ""),
                        "type": item_type,
                    },
                ))

                if host_node:
                    edges.append(TopologyEdge(
                        from_entity=entity_id,
                        to_entity=f"proxmox:{host_node}",
                        edge_type="runs_on",
                        source=source,
                        last_seen=now,
                    ))

        return nodes, edges
