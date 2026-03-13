"""Docker handler — executes actions via the Docker Engine REST API.

Operations: restart_container, get_container_logs, get_container_stats,
inspect_container.

Connection: Unix socket (default) or TCP with optional TLS.
Uses aiohttp with UnixConnector for socket access.

ARCHITECTURE.md §8 and §16.6 define the handler interface and operations.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

import aiohttp

from oasisagent.handlers.base import Handler
from oasisagent.models import ActionResult, ActionStatus, VerifyResult

if TYPE_CHECKING:
    from oasisagent.config import DockerHandlerConfig
    from oasisagent.models import Event, RecommendedAction

logger = logging.getLogger(__name__)

# Operations this handler supports. can_handle() whitelists these.
_KNOWN_OPERATIONS: frozenset[str] = frozenset({
    "restart_container",
    "get_container_logs",
    "get_container_stats",
    "inspect_container",
})


class HandlerNotStartedError(Exception):
    """Raised when handler methods are called before start()."""


class DockerHandler(Handler):
    """Executes actions against Docker via the Engine REST API.

    Must be started with ``await handler.start()`` before use.
    Supports unix socket (default) and TCP connections.
    """

    def __init__(self, config: DockerHandlerConfig) -> None:
        self._config = config
        self._session: aiohttp.ClientSession | None = None

    def name(self) -> str:
        return "docker"

    async def start(self) -> None:
        """Create the aiohttp session with the appropriate connector."""
        if self._config.url is not None:
            # TCP mode
            ssl_context: bool | None = None
            if self._config.url.startswith("https"):
                ssl_context = self._config.tls_verify if self._config.tls_verify else False
            connector = aiohttp.TCPConnector(ssl=ssl_context)
            self._session = aiohttp.ClientSession(
                base_url=self._config.url,
                connector=connector,
            )
            logger.info("Docker handler started (mode=tcp, url=%s)", self._config.url)
        else:
            # Unix socket mode
            socket_path = self._config.socket.removeprefix("unix://")
            connector = aiohttp.UnixConnector(path=socket_path)
            self._session = aiohttp.ClientSession(
                connector=connector,
                base_url="http://localhost",
            )
            logger.info("Docker handler started (mode=unix, socket=%s)", socket_path)

    async def stop(self) -> None:
        """Close the aiohttp session."""
        if self._session is not None:
            await self._session.close()
            self._session = None
            logger.info("Docker handler stopped")

    async def healthy(self) -> bool:
        """Check Docker connectivity via GET /_ping."""
        if self._session is None:
            return False
        async with self._session.get("/_ping") as resp:
            body = await resp.text()
            return body == "OK"

    async def can_handle(self, event: Event, action: RecommendedAction) -> bool:
        """Check if this handler supports the action."""
        return (
            action.handler == "docker"
            and action.operation in _KNOWN_OPERATIONS
        )

    async def execute(
        self, event: Event, action: RecommendedAction
    ) -> ActionResult:
        """Dispatch to the appropriate operation method."""
        self._ensure_started()

        dispatch = {
            "restart_container": self._op_restart_container,
            "get_container_logs": self._op_get_container_logs,
            "get_container_stats": self._op_get_container_stats,
            "inspect_container": self._op_inspect_container,
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
                "Docker handler HTTP error for %s: %s", action.operation, exc
            )
            return ActionResult(
                status=ActionStatus.FAILURE,
                error_message=f"HTTP error: {exc}",
                duration_ms=elapsed,
            )

    async def verify(
        self, event: Event, action: RecommendedAction, result: ActionResult
    ) -> VerifyResult:
        """Verify an action had the desired effect.

        Only restart_container has real verification (polls container status).
        Other operations return verified=True immediately.
        """
        if action.operation != "restart_container":
            return VerifyResult(verified=True, message="No verification needed")

        self._ensure_started()
        container_id = (
            result.details.get("container_id")
            or action.params.get("container_id")
            or event.entity_id
        )
        return await self._verify_container_running(container_id)

    async def get_context(self, event: Event) -> dict[str, Any]:
        """Gather Docker-specific context for diagnosis.

        Fetches both container inspect data (state, config, restart count,
        OOM flag, exit code, health check config) and recent logs.
        """
        self._ensure_started()
        context: dict[str, Any] = {}
        container_id = event.entity_id

        try:
            assert self._session is not None
            async with self._session.get(
                f"/containers/{container_id}/json"
            ) as resp:
                resp.raise_for_status()
                context["container_inspect"] = await resp.json()
        except aiohttp.ClientError as exc:
            context["container_inspect_error"] = str(exc)

        try:
            assert self._session is not None
            async with self._session.get(
                f"/containers/{container_id}/logs",
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

    async def _op_restart_container(
        self, event: Event, action: RecommendedAction
    ) -> ActionResult:
        """Restart a container via POST /containers/{id}/restart."""
        container_id = action.params.get("container_id") or event.entity_id
        if not container_id:
            return ActionResult(
                status=ActionStatus.FAILURE,
                error_message="restart_container requires container_id in params or entity_id",
            )

        assert self._session is not None
        params: dict[str, str] = {}
        timeout = action.params.get("timeout")
        if timeout is not None:
            params["t"] = str(timeout)

        async with self._session.post(
            f"/containers/{container_id}/restart", params=params
        ) as resp:
            resp.raise_for_status()

        logger.info("Docker restart_container: %s", container_id)
        return ActionResult(
            status=ActionStatus.SUCCESS,
            details={"container_id": container_id},
        )

    async def _op_get_container_logs(
        self, event: Event, action: RecommendedAction
    ) -> ActionResult:
        """Fetch container logs via GET /containers/{id}/logs."""
        container_id = action.params.get("container_id") or event.entity_id
        if not container_id:
            return ActionResult(
                status=ActionStatus.FAILURE,
                error_message="get_container_logs requires container_id in params or entity_id",
            )

        tail = action.params.get("tail", "100")
        assert self._session is not None
        async with self._session.get(
            f"/containers/{container_id}/logs",
            params={"tail": str(tail), "stdout": "true", "stderr": "true"},
        ) as resp:
            resp.raise_for_status()
            logs = await resp.text()

        return ActionResult(
            status=ActionStatus.SUCCESS,
            details={"container_id": container_id, "logs": logs},
        )

    async def _op_get_container_stats(
        self, event: Event, action: RecommendedAction
    ) -> ActionResult:
        """Fetch container stats via GET /containers/{id}/stats."""
        container_id = action.params.get("container_id") or event.entity_id
        if not container_id:
            return ActionResult(
                status=ActionStatus.FAILURE,
                error_message="get_container_stats requires container_id in params or entity_id",
            )

        assert self._session is not None
        async with self._session.get(
            f"/containers/{container_id}/stats",
            params={"stream": "false"},
        ) as resp:
            resp.raise_for_status()
            stats = await resp.json()

        return ActionResult(
            status=ActionStatus.SUCCESS,
            details={"container_id": container_id, "stats": stats},
        )

    async def _op_inspect_container(
        self, event: Event, action: RecommendedAction
    ) -> ActionResult:
        """Inspect a container via GET /containers/{id}/json."""
        container_id = action.params.get("container_id") or event.entity_id
        if not container_id:
            return ActionResult(
                status=ActionStatus.FAILURE,
                error_message="inspect_container requires container_id in params or entity_id",
            )

        assert self._session is not None
        async with self._session.get(
            f"/containers/{container_id}/json"
        ) as resp:
            resp.raise_for_status()
            inspect_data = await resp.json()

        return ActionResult(
            status=ActionStatus.SUCCESS,
            details={"container_id": container_id, "inspect": inspect_data},
        )

    # -------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------

    def _ensure_started(self) -> None:
        """Raise if the handler hasn't been started."""
        if self._session is None:
            raise HandlerNotStartedError(
                "DockerHandler.start() must be called before use"
            )

    async def _verify_container_running(self, container_id: str) -> VerifyResult:
        """Poll container status to check if it reaches 'running'."""
        timeout = self._config.verify_timeout
        interval = self._config.verify_poll_interval
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            try:
                assert self._session is not None
                async with self._session.get(
                    f"/containers/{container_id}/json"
                ) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
                    status = data.get("State", {}).get("Status", "unknown")
                    if status == "running":
                        return VerifyResult(
                            verified=True,
                            message=f"Container {container_id} is running",
                        )
            except aiohttp.ClientError:
                pass  # Keep polling

            await asyncio.sleep(interval)

        return VerifyResult(
            verified=False,
            message=(
                f"Container {container_id} did not reach 'running' within "
                f"{timeout}s"
            ),
        )
