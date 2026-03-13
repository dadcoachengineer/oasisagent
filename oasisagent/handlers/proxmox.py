"""Proxmox VE handler — executes actions via the Proxmox REST API.

Operations: notify, get_node_status, list_vms, get_vm_status,
start_vm, stop_vm (graceful ACPI shutdown), reboot_vm, list_tasks,
get_task_log.

Connection: HTTPS (port 8006) with API token auth. TLS verification
is optional (``verify_ssl: false`` for self-signed certs).

Auth header format: ``Authorization: PVEAPIToken=USER@REALM!TOKENID=UUID``

ARCHITECTURE.md §8 and §16.7 define the handler interface and operations.
"""

from __future__ import annotations

import asyncio
import logging
import ssl
import time
from typing import TYPE_CHECKING, Any

import aiohttp

from oasisagent.handlers.base import Handler
from oasisagent.models import ActionResult, ActionStatus, VerifyResult

if TYPE_CHECKING:
    from oasisagent.config import ProxmoxHandlerConfig
    from oasisagent.models import Event, RecommendedAction

logger = logging.getLogger(__name__)

# Operations this handler supports. can_handle() whitelists these.
_KNOWN_OPERATIONS: frozenset[str] = frozenset({
    "notify",
    "get_node_status",
    "list_vms",
    "get_vm_status",
    "start_vm",
    "stop_vm",
    "reboot_vm",
    "list_tasks",
    "get_task_log",
})

# Default inventory cache TTL in seconds.
_INVENTORY_TTL = 300

# Health check cache TTL and per-request timeout.
_HEALTH_CACHE_TTL = 30.0  # seconds
_HEALTH_TIMEOUT = aiohttp.ClientTimeout(total=3)


class HandlerNotStartedError(Exception):
    """Raised when handler methods are called before start()."""


class ProxmoxHandler(Handler):
    """Executes actions against Proxmox VE via the REST API.

    Must be started with ``await handler.start()`` before use.
    Maintains an in-memory cluster resource inventory that is refreshed
    periodically (TTL-based) and on cache miss during execute().
    """

    def __init__(self, config: ProxmoxHandlerConfig) -> None:
        self._config = config
        self._session: aiohttp.ClientSession | None = None
        # Cluster resource inventory: vmid → {node, type, name, status, ...}
        self._inventory: dict[str, dict[str, Any]] = {}
        self._inventory_ts: float = 0.0
        # Health check cache
        self._health_cache: bool | None = None
        self._health_cache_ts: float = 0.0

    def name(self) -> str:
        return "proxmox"

    async def start(self) -> None:
        """Create the aiohttp session and populate the cluster inventory."""
        ssl_context: ssl.SSLContext | bool = False
        if self._config.verify_ssl:
            ssl_context = ssl.create_default_context()

        auth_value = (
            f"PVEAPIToken={self._config.user}!{self._config.token_name}"
            f"={self._config.token_value}"
        )
        headers = {"Authorization": auth_value}

        connector = aiohttp.TCPConnector(ssl=ssl_context)
        self._session = aiohttp.ClientSession(
            base_url=self._config.url,
            connector=connector,
            headers=headers,
        )
        logger.info(
            "Proxmox handler started (url=%s, user=%s)",
            self._config.url, self._config.user,
        )

        try:
            await self._refresh_inventory()
        except (aiohttp.ClientError, TimeoutError):
            logger.warning(
                "Proxmox handler: initial inventory refresh failed — "
                "will retry on first use"
            )

    async def stop(self) -> None:
        """Close the aiohttp session."""
        if self._session is not None:
            await self._session.close()
            self._session = None
            self._health_cache = None
            self._health_cache_ts = 0.0
            logger.info("Proxmox handler stopped")

    async def healthy(self) -> bool:
        """Check Proxmox connectivity via GET /api2/json/version.

        Uses a 30s TTL cache to avoid hammering an unreachable host.
        Internal 3s timeout fits inside the orchestrator's 5s envelope.
        """
        if self._session is None:
            return False

        now = time.monotonic()
        if self._health_cache is not None and (now - self._health_cache_ts) < _HEALTH_CACHE_TTL:
            return self._health_cache

        try:
            async with self._session.get(
                "/api2/json/version", timeout=_HEALTH_TIMEOUT,
            ) as resp:
                result = resp.status == 200
                if not result:
                    logger.warning(
                        "Proxmox health check: HTTP %d (%s)",
                        resp.status, self._config.url,
                    )
        except aiohttp.ClientConnectorError as exc:
            logger.warning(
                "Proxmox health check: connection failed (%s): %s",
                self._config.url, exc,
            )
            result = False
        except TimeoutError:
            logger.warning(
                "Proxmox health check: timed out after 3s (%s)",
                self._config.url,
            )
            result = False
        except aiohttp.ClientError as exc:
            logger.warning(
                "Proxmox health check failed (%s): %s",
                self._config.url, exc,
            )
            result = False

        self._health_cache = result
        self._health_cache_ts = now
        return result

    async def can_handle(self, event: Event, action: RecommendedAction) -> bool:
        return (
            action.handler == "proxmox"
            and action.operation in _KNOWN_OPERATIONS
        )

    async def execute(
        self, event: Event, action: RecommendedAction,
    ) -> ActionResult:
        """Dispatch to the appropriate operation method."""
        self._ensure_started()

        dispatch = {
            "notify": self._op_notify,
            "get_node_status": self._op_get_node_status,
            "list_vms": self._op_list_vms,
            "get_vm_status": self._op_get_vm_status,
            "start_vm": self._op_start_vm,
            "stop_vm": self._op_stop_vm,
            "reboot_vm": self._op_reboot_vm,
            "list_tasks": self._op_list_tasks,
            "get_task_log": self._op_get_task_log,
        }

        handler_fn = dispatch.get(action.operation)
        if handler_fn is None:
            return ActionResult(
                status=ActionStatus.FAILURE,
                error_message=f"Unknown operation: {action.operation}",
            )

        start = time.monotonic()
        try:
            result = await handler_fn(event, action)
            elapsed = (time.monotonic() - start) * 1000
            return result.model_copy(update={"duration_ms": elapsed})
        except aiohttp.ClientError as exc:
            elapsed = (time.monotonic() - start) * 1000
            logger.error(
                "Proxmox handler HTTP error for %s: %s", action.operation, exc,
            )
            return ActionResult(
                status=ActionStatus.FAILURE,
                error_message=f"HTTP error: {exc}",
                duration_ms=elapsed,
            )

    async def verify(
        self, event: Event, action: RecommendedAction, result: ActionResult,
    ) -> VerifyResult:
        """Verify an action had the desired effect.

        start_vm and stop_vm poll the VM/CT status until the desired state
        is reached. Other operations return verified=True immediately.
        """
        if action.operation not in ("start_vm", "stop_vm", "reboot_vm"):
            return VerifyResult(verified=True, message="No verification needed")

        self._ensure_started()

        vmid = result.details.get("vmid") or action.params.get("vmid") or event.entity_id
        node = result.details.get("node") or action.params.get("node")
        vm_type = result.details.get("vm_type") or action.params.get("type", "qemu")

        if not node:
            return VerifyResult(
                verified=False, message="Cannot verify: node not known",
            )

        expected = "running" if action.operation in ("start_vm", "reboot_vm") else "stopped"
        return await self._verify_vm_status(node, vm_type, vmid, expected)

    async def get_context(self, event: Event) -> dict[str, Any]:
        """Gather Proxmox-specific context for T1/T2 diagnosis.

        Fetches cluster status, node stats, VM/CT status for the
        relevant entity, and recent tasks.
        """
        self._ensure_started()
        context: dict[str, Any] = {}

        # Refresh inventory if stale
        await self._ensure_inventory()

        # Cluster status
        try:
            cluster_status = await self._api_get("/api2/json/cluster/status")
            context["cluster_status"] = cluster_status
        except aiohttp.ClientError as exc:
            context["cluster_status_error"] = str(exc)

        # Look up entity in inventory
        entity_id = event.entity_id
        inv_entry = self._inventory.get(entity_id)
        if inv_entry:
            context["resource_info"] = inv_entry
            node = inv_entry.get("node", "")
            vm_type = inv_entry.get("type", "")
            vmid = inv_entry.get("vmid", entity_id)

            # Fetch current status
            if vm_type in ("qemu", "lxc"):
                try:
                    status = await self._api_get(
                        f"/api2/json/nodes/{node}/{vm_type}/{vmid}/status/current",
                    )
                    context["vm_status"] = status
                except aiohttp.ClientError as exc:
                    context["vm_status_error"] = str(exc)

        # Recent tasks
        try:
            tasks = await self._api_get(
                "/api2/json/cluster/tasks",
                params={"limit": "20"},
            )
            context["recent_tasks"] = tasks
        except aiohttp.ClientError as exc:
            context["recent_tasks_error"] = str(exc)

        return context

    # -------------------------------------------------------------------
    # Operation implementations
    # -------------------------------------------------------------------

    async def _op_notify(
        self, event: Event, action: RecommendedAction,
    ) -> ActionResult:
        """Notify — no system changes. Returns the diagnosis message."""
        message = action.params.get("message", action.description)
        logger.info("Proxmox notify: %s (entity=%s)", message, event.entity_id)
        return ActionResult(
            status=ActionStatus.SUCCESS,
            details={"message": message, "entity_id": event.entity_id},
        )

    async def _op_get_node_status(
        self, event: Event, action: RecommendedAction,
    ) -> ActionResult:
        """Fetch node status via GET /nodes/{node}/status."""
        node = action.params.get("node") or event.entity_id
        if not node:
            return ActionResult(
                status=ActionStatus.FAILURE,
                error_message="get_node_status requires 'node' in params or entity_id",
            )

        data = await self._api_get(f"/api2/json/nodes/{node}/status")
        return ActionResult(
            status=ActionStatus.SUCCESS,
            details={"node": node, "status": data},
        )

    async def _op_list_vms(
        self, event: Event, action: RecommendedAction,
    ) -> ActionResult:
        """List VMs and containers from the cluster inventory."""
        await self._ensure_inventory()
        vms = [
            entry for entry in self._inventory.values()
            if entry.get("type") in ("qemu", "lxc")
        ]
        return ActionResult(
            status=ActionStatus.SUCCESS,
            details={"vms": vms, "count": len(vms)},
        )

    async def _op_get_vm_status(
        self, event: Event, action: RecommendedAction,
    ) -> ActionResult:
        """Fetch current VM/CT status."""
        vmid = action.params.get("vmid") or event.entity_id
        node, vm_type = await self._resolve_vm(vmid, action.params)

        if not node:
            return ActionResult(
                status=ActionStatus.FAILURE,
                error_message=f"Cannot resolve node for VM {vmid}. "
                "Provide 'node' in params or ensure VM is in inventory.",
            )

        data = await self._api_get(
            f"/api2/json/nodes/{node}/{vm_type}/{vmid}/status/current",
        )
        return ActionResult(
            status=ActionStatus.SUCCESS,
            details={"vmid": vmid, "node": node, "vm_type": vm_type, "status": data},
        )

    async def _op_start_vm(
        self, event: Event, action: RecommendedAction,
    ) -> ActionResult:
        """Start a VM/CT via POST /nodes/{node}/{type}/{vmid}/status/start."""
        vmid = action.params.get("vmid") or event.entity_id
        node, vm_type = await self._resolve_vm(vmid, action.params)

        if not node:
            return ActionResult(
                status=ActionStatus.FAILURE,
                error_message=f"Cannot resolve node for VM {vmid}",
            )

        upid = await self._api_post(
            f"/api2/json/nodes/{node}/{vm_type}/{vmid}/status/start",
        )
        logger.info("Proxmox start_vm: vmid=%s node=%s upid=%s", vmid, node, upid)
        return ActionResult(
            status=ActionStatus.SUCCESS,
            details={"vmid": vmid, "node": node, "vm_type": vm_type, "upid": upid},
        )

    async def _op_stop_vm(
        self, event: Event, action: RecommendedAction,
    ) -> ActionResult:
        """Gracefully stop a VM/CT via ACPI shutdown.

        Uses the ``shutdown`` endpoint (ACPI signal) rather than ``stop``
        (immediate power-off). A future ``force_stop_vm`` operation could
        use the ``stop`` endpoint for hard power-off.
        """
        vmid = action.params.get("vmid") or event.entity_id
        node, vm_type = await self._resolve_vm(vmid, action.params)

        if not node:
            return ActionResult(
                status=ActionStatus.FAILURE,
                error_message=f"Cannot resolve node for VM {vmid}",
            )

        upid = await self._api_post(
            f"/api2/json/nodes/{node}/{vm_type}/{vmid}/status/shutdown",
        )
        logger.info("Proxmox stop_vm (shutdown): vmid=%s node=%s upid=%s", vmid, node, upid)
        return ActionResult(
            status=ActionStatus.SUCCESS,
            details={"vmid": vmid, "node": node, "vm_type": vm_type, "upid": upid},
        )

    async def _op_reboot_vm(
        self, event: Event, action: RecommendedAction,
    ) -> ActionResult:
        """Reboot a VM/CT via POST /nodes/{node}/{type}/{vmid}/status/reboot."""
        vmid = action.params.get("vmid") or event.entity_id
        node, vm_type = await self._resolve_vm(vmid, action.params)

        if not node:
            return ActionResult(
                status=ActionStatus.FAILURE,
                error_message=f"Cannot resolve node for VM {vmid}",
            )

        upid = await self._api_post(
            f"/api2/json/nodes/{node}/{vm_type}/{vmid}/status/reboot",
        )
        logger.info("Proxmox reboot_vm: vmid=%s node=%s upid=%s", vmid, node, upid)
        return ActionResult(
            status=ActionStatus.SUCCESS,
            details={"vmid": vmid, "node": node, "vm_type": vm_type, "upid": upid},
        )

    async def _op_list_tasks(
        self, event: Event, action: RecommendedAction,
    ) -> ActionResult:
        """List recent cluster tasks."""
        limit = action.params.get("limit", "50")
        data = await self._api_get(
            "/api2/json/cluster/tasks",
            params={"limit": str(limit)},
        )
        return ActionResult(
            status=ActionStatus.SUCCESS,
            details={"tasks": data, "count": len(data) if isinstance(data, list) else 0},
        )

    async def _op_get_task_log(
        self, event: Event, action: RecommendedAction,
    ) -> ActionResult:
        """Fetch the log output for a specific task UPID."""
        upid = action.params.get("upid")
        node = action.params.get("node")
        if not upid or not node:
            return ActionResult(
                status=ActionStatus.FAILURE,
                error_message="get_task_log requires 'upid' and 'node' in params",
            )

        data = await self._api_get(
            f"/api2/json/nodes/{node}/tasks/{upid}/log",
            params={"limit": "500"},
        )
        return ActionResult(
            status=ActionStatus.SUCCESS,
            details={"upid": upid, "log": data},
        )

    # -------------------------------------------------------------------
    # Cluster inventory
    # -------------------------------------------------------------------

    async def _refresh_inventory(self) -> None:
        """Fetch /cluster/resources and populate the inventory cache."""
        data = await self._api_get(
            "/api2/json/cluster/resources", params={"type": "vm"},
        )
        inventory: dict[str, dict[str, Any]] = {}
        if isinstance(data, list):
            for resource in data:
                vmid = str(resource.get("vmid", ""))
                if vmid:
                    inventory[vmid] = resource
        self._inventory = inventory
        self._inventory_ts = time.monotonic()
        logger.debug("Proxmox inventory refreshed: %d VMs/CTs", len(inventory))

    async def _ensure_inventory(self) -> None:
        """Refresh inventory if TTL has expired."""
        if time.monotonic() - self._inventory_ts > _INVENTORY_TTL:
            try:
                await self._refresh_inventory()
            except (aiohttp.ClientError, TimeoutError):
                logger.warning("Proxmox inventory refresh failed, using stale cache")

    async def _resolve_vm(
        self, vmid: str, params: dict[str, Any],
    ) -> tuple[str, str]:
        """Resolve a VM's node and type from params or inventory.

        Returns (node, vm_type). Tries params first, then inventory cache,
        then refreshes inventory once on cache miss.
        """
        node = params.get("node", "")
        vm_type = params.get("type", "")

        if node and vm_type:
            return node, vm_type

        # Check inventory
        inv = self._inventory.get(vmid)
        if inv:
            return inv.get("node", node), inv.get("type", vm_type or "qemu")

        # Cache miss — refresh once and retry
        try:
            await self._refresh_inventory()
        except (aiohttp.ClientError, TimeoutError):
            logger.warning("Proxmox inventory refresh failed during VM resolve")
            return node, vm_type or "qemu"

        inv = self._inventory.get(vmid)
        if inv:
            return inv.get("node", node), inv.get("type", vm_type or "qemu")

        return node, vm_type or "qemu"

    # -------------------------------------------------------------------
    # HTTP helpers
    # -------------------------------------------------------------------

    async def _api_get(
        self, path: str, params: dict[str, str] | None = None,
    ) -> dict[str, Any] | list[dict[str, Any]]:
        """Perform a GET request and return the ``data`` field."""
        assert self._session is not None
        async with self._session.get(path, params=params) as resp:
            resp.raise_for_status()
            body = await resp.json()
            return body.get("data", body)

    async def _api_post(
        self, path: str, json_data: dict[str, Any] | None = None,
    ) -> dict[str, Any] | list[dict[str, Any]] | str:
        """Perform a POST request and return the ``data`` field."""
        assert self._session is not None
        async with self._session.post(path, json=json_data) as resp:
            resp.raise_for_status()
            body = await resp.json()
            return body.get("data", body)

    def _ensure_started(self) -> None:
        """Raise if the handler hasn't been started."""
        if self._session is None:
            raise HandlerNotStartedError(
                "ProxmoxHandler.start() must be called before use",
            )

    async def _verify_vm_status(
        self, node: str, vm_type: str, vmid: str, expected: str,
    ) -> VerifyResult:
        """Poll VM/CT status until it matches the expected state."""
        timeout = 30
        interval = 2
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            try:
                data = await self._api_get(
                    f"/api2/json/nodes/{node}/{vm_type}/{vmid}/status/current",
                )
                status = data.get("status", "unknown") if isinstance(data, dict) else "unknown"
                if status == expected:
                    return VerifyResult(
                        verified=True,
                        message=f"VM {vmid} is {expected}",
                    )
            except aiohttp.ClientError:
                pass  # Keep polling

            await asyncio.sleep(interval)

        return VerifyResult(
            verified=False,
            message=f"VM {vmid} did not reach '{expected}' within {timeout}s",
        )
