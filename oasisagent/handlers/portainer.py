"""Portainer handler — manages Docker containers via the Portainer REST API.

Operations: notify, restart_container, stop_container, start_container,
get_container_logs, get_container_stats, inspect_container, list_containers.

Portainer proxies Docker API calls through its environment-scoped endpoints:
``/api/endpoints/{endpoint_id}/docker/containers/...``

Auth: API key via ``X-API-Key`` header.

ARCHITECTURE.md §8 defines the handler interface.
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
    from oasisagent.config import PortainerHandlerConfig
    from oasisagent.models import Event, RecommendedAction

logger = logging.getLogger(__name__)

# Operations this handler supports.
_KNOWN_OPERATIONS: frozenset[str] = frozenset({
    "notify",
    "restart_container",
    "stop_container",
    "start_container",
    "get_container_logs",
    "get_container_stats",
    "inspect_container",
    "list_containers",
})


class HandlerNotStartedError(Exception):
    """Raised when handler methods are called before start()."""


class PortainerHandler(Handler):
    """Manages Docker containers via the Portainer REST API.

    Must be started with ``await handler.start()`` before use.
    All container operations are proxied through Portainer's environment-scoped
    Docker API at ``/api/endpoints/{endpoint_id}/docker/...``.
    """

    def __init__(self, config: PortainerHandlerConfig) -> None:
        self._config = config
        self._session: aiohttp.ClientSession | None = None
        self._docker_prefix = (
            f"/api/endpoints/{config.endpoint_id}/docker"
        )

    def _docker_prefix_for(
        self, event: Event, action: RecommendedAction | None = None,
    ) -> str:
        """Resolve Docker API prefix with per-request endpoint routing.

        Checks action.params["endpoint_id"] and event.payload["endpoint_id"]
        before falling back to the config default.
        """
        ep_id = None
        if action is not None:
            ep_id = action.params.get("endpoint_id")
        if ep_id is None:
            ep_id = event.payload.get("endpoint_id")
        if ep_id is not None:
            return f"/api/endpoints/{ep_id}/docker"
        return self._docker_prefix

    def name(self) -> str:
        return "portainer"

    async def start(self) -> None:
        """Create the aiohttp session with API key auth."""
        ssl_context: ssl.SSLContext | bool = False
        if self._config.verify_ssl:
            ssl_context = ssl.create_default_context()

        headers = {"X-API-Key": self._config.api_key}
        connector = aiohttp.TCPConnector(ssl=ssl_context)
        self._session = aiohttp.ClientSession(
            base_url=self._config.url,
            connector=connector,
            headers=headers,
        )
        logger.info(
            "Portainer handler started (url=%s, endpoint_id=%d)",
            self._config.url, self._config.endpoint_id,
        )

    async def stop(self) -> None:
        """Close the aiohttp session."""
        if self._session is not None:
            await self._session.close()
            self._session = None
            logger.info("Portainer handler stopped")

    async def healthy(self) -> bool:
        """Check Portainer connectivity via GET /api/status."""
        if self._session is None:
            return False
        async with self._session.get("/api/status") as resp:
            return resp.status == 200

    async def can_handle(self, event: Event, action: RecommendedAction) -> bool:
        return (
            action.handler == "portainer"
            and action.operation in _KNOWN_OPERATIONS
        )

    async def execute(
        self, event: Event, action: RecommendedAction,
    ) -> ActionResult:
        """Dispatch to the appropriate operation method."""
        self._ensure_started()

        dispatch = {
            "notify": self._op_notify,
            "restart_container": self._op_restart_container,
            "stop_container": self._op_stop_container,
            "start_container": self._op_start_container,
            "get_container_logs": self._op_get_container_logs,
            "get_container_stats": self._op_get_container_stats,
            "inspect_container": self._op_inspect_container,
            "list_containers": self._op_list_containers,
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
                "Portainer handler HTTP error for %s: %s", action.operation, exc,
            )
            return ActionResult(
                status=ActionStatus.FAILURE,
                error_message=f"HTTP error: {exc}",
                duration_ms=elapsed,
            )

    async def verify(
        self, event: Event, action: RecommendedAction, result: ActionResult,
    ) -> VerifyResult:
        """Verify a container lifecycle action had the desired effect.

        restart_container and start_container poll for "running".
        stop_container polls for "exited" or "stopped".
        Other operations return verified=True immediately.
        """
        if action.operation not in (
            "restart_container", "start_container", "stop_container",
        ):
            return VerifyResult(verified=True, message="No verification needed")

        self._ensure_started()

        container_id = (
            result.details.get("container_id")
            or action.params.get("container_id")
            or event.entity_id
        )

        expected = ("exited", "stopped") if action.operation == "stop_container" else ("running",)
        prefix = self._docker_prefix_for(event, action)

        return await self._verify_container_status(
            container_id, expected, docker_prefix=prefix,
        )

    async def get_context(self, event: Event) -> dict[str, Any]:
        """Gather Docker-specific context via Portainer for T1/T2 diagnosis.

        Fetches container inspect data and recent logs.
        """
        self._ensure_started()
        context: dict[str, Any] = {}
        container_id = event.entity_id
        prefix = self._docker_prefix_for(event)

        try:
            assert self._session is not None
            async with self._session.get(
                f"{prefix}/containers/{container_id}/json",
            ) as resp:
                resp.raise_for_status()
                context["container_inspect"] = await resp.json()
        except aiohttp.ClientError as exc:
            context["container_inspect_error"] = str(exc)

        try:
            assert self._session is not None
            async with self._session.get(
                f"{prefix}/containers/{container_id}/logs",
                params={"tail": "100", "stdout": "true", "stderr": "true"},
            ) as resp:
                resp.raise_for_status()
                context["container_logs"] = await resp.text()
        except aiohttp.ClientError as exc:
            context["container_logs_error"] = str(exc)

        return context

    # -------------------------------------------------------------------
    # Operation implementations
    # -------------------------------------------------------------------

    async def _op_notify(
        self, event: Event, action: RecommendedAction,
    ) -> ActionResult:
        """Notify — no system changes. Returns the diagnosis message."""
        message = action.params.get("message", action.description)
        logger.info("Portainer notify: %s (entity=%s)", message, event.entity_id)
        return ActionResult(
            status=ActionStatus.SUCCESS,
            details={"message": message, "entity_id": event.entity_id},
        )

    async def _op_restart_container(
        self, event: Event, action: RecommendedAction,
    ) -> ActionResult:
        """Restart a container via Portainer-proxied Docker API."""
        container_id = action.params.get("container_id") or event.entity_id
        if not container_id:
            return ActionResult(
                status=ActionStatus.FAILURE,
                error_message="restart_container requires container_id in params or entity_id",
            )

        prefix = self._docker_prefix_for(event, action)
        assert self._session is not None
        async with self._session.post(
            f"{prefix}/containers/{container_id}/restart",
        ) as resp:
            resp.raise_for_status()

        logger.info("Portainer restart_container: %s", container_id)
        return ActionResult(
            status=ActionStatus.SUCCESS,
            details={"container_id": container_id},
        )

    async def _op_stop_container(
        self, event: Event, action: RecommendedAction,
    ) -> ActionResult:
        """Stop a container via Portainer-proxied Docker API."""
        container_id = action.params.get("container_id") or event.entity_id
        if not container_id:
            return ActionResult(
                status=ActionStatus.FAILURE,
                error_message="stop_container requires container_id in params or entity_id",
            )

        prefix = self._docker_prefix_for(event, action)
        assert self._session is not None
        params: dict[str, str] = {}
        timeout = action.params.get("timeout")
        if timeout is not None:
            params["t"] = str(timeout)

        async with self._session.post(
            f"{prefix}/containers/{container_id}/stop",
            params=params,
        ) as resp:
            resp.raise_for_status()

        logger.info("Portainer stop_container: %s", container_id)
        return ActionResult(
            status=ActionStatus.SUCCESS,
            details={"container_id": container_id},
        )

    async def _op_start_container(
        self, event: Event, action: RecommendedAction,
    ) -> ActionResult:
        """Start a container via Portainer-proxied Docker API."""
        container_id = action.params.get("container_id") or event.entity_id
        if not container_id:
            return ActionResult(
                status=ActionStatus.FAILURE,
                error_message="start_container requires container_id in params or entity_id",
            )

        prefix = self._docker_prefix_for(event, action)
        assert self._session is not None
        async with self._session.post(
            f"{prefix}/containers/{container_id}/start",
        ) as resp:
            resp.raise_for_status()

        logger.info("Portainer start_container: %s", container_id)
        return ActionResult(
            status=ActionStatus.SUCCESS,
            details={"container_id": container_id},
        )

    async def _op_get_container_logs(
        self, event: Event, action: RecommendedAction,
    ) -> ActionResult:
        """Fetch container logs via Portainer-proxied Docker API."""
        container_id = action.params.get("container_id") or event.entity_id
        if not container_id:
            return ActionResult(
                status=ActionStatus.FAILURE,
                error_message="get_container_logs requires container_id in params or entity_id",
            )

        prefix = self._docker_prefix_for(event, action)
        tail = action.params.get("tail", "100")
        assert self._session is not None
        async with self._session.get(
            f"{prefix}/containers/{container_id}/logs",
            params={"tail": str(tail), "stdout": "true", "stderr": "true"},
        ) as resp:
            resp.raise_for_status()
            logs = await resp.text()

        return ActionResult(
            status=ActionStatus.SUCCESS,
            details={"container_id": container_id, "logs": logs},
        )

    async def _op_get_container_stats(
        self, event: Event, action: RecommendedAction,
    ) -> ActionResult:
        """Fetch container stats via Portainer-proxied Docker API."""
        container_id = action.params.get("container_id") or event.entity_id
        if not container_id:
            return ActionResult(
                status=ActionStatus.FAILURE,
                error_message="get_container_stats requires container_id in params or entity_id",
            )

        prefix = self._docker_prefix_for(event, action)
        assert self._session is not None
        async with self._session.get(
            f"{prefix}/containers/{container_id}/stats",
            params={"stream": "false"},
        ) as resp:
            resp.raise_for_status()
            stats = await resp.json()

        return ActionResult(
            status=ActionStatus.SUCCESS,
            details={"container_id": container_id, "stats": stats},
        )

    async def _op_inspect_container(
        self, event: Event, action: RecommendedAction,
    ) -> ActionResult:
        """Inspect a container via Portainer-proxied Docker API."""
        container_id = action.params.get("container_id") or event.entity_id
        if not container_id:
            return ActionResult(
                status=ActionStatus.FAILURE,
                error_message="inspect_container requires container_id in params or entity_id",
            )

        prefix = self._docker_prefix_for(event, action)
        assert self._session is not None
        async with self._session.get(
            f"{prefix}/containers/{container_id}/json",
        ) as resp:
            resp.raise_for_status()
            inspect_data = await resp.json()

        return ActionResult(
            status=ActionStatus.SUCCESS,
            details={"container_id": container_id, "inspect": inspect_data},
        )

    async def _op_list_containers(
        self, event: Event, action: RecommendedAction,
    ) -> ActionResult:
        """List containers in the Portainer environment."""
        all_containers = action.params.get("all", "false")
        prefix = self._docker_prefix_for(event, action)
        assert self._session is not None
        async with self._session.get(
            f"{prefix}/containers/json",
            params={"all": str(all_containers).lower()},
        ) as resp:
            resp.raise_for_status()
            containers = await resp.json()

        return ActionResult(
            status=ActionStatus.SUCCESS,
            details={
                "containers": containers,
                "count": len(containers) if isinstance(containers, list) else 0,
            },
        )

    # -------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------

    def _ensure_started(self) -> None:
        """Raise if the handler hasn't been started."""
        if self._session is None:
            raise HandlerNotStartedError(
                "PortainerHandler.start() must be called before use",
            )

    async def _verify_container_status(
        self,
        container_id: str,
        expected: tuple[str, ...],
        docker_prefix: str | None = None,
    ) -> VerifyResult:
        """Poll container status until it matches one of the expected states."""
        prefix = docker_prefix or self._docker_prefix
        timeout = self._config.verify_timeout
        interval = self._config.verify_poll_interval
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            try:
                assert self._session is not None
                async with self._session.get(
                    f"{prefix}/containers/{container_id}/json",
                ) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
                    status = data.get("State", {}).get("Status", "unknown")
                    if status in expected:
                        return VerifyResult(
                            verified=True,
                            message=f"Container {container_id} is {status}",
                        )
            except aiohttp.ClientError:
                pass  # Keep polling

            await asyncio.sleep(interval)

        return VerifyResult(
            verified=False,
            message=(
                f"Container {container_id} did not reach "
                f"{'/'.join(expected)} within {timeout}s"
            ),
        )
