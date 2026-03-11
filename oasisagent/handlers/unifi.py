"""UniFi Network handler — executes actions via the UniFi controller API.

Operations: notify, restart_device, block_client, unblock_client.

Uses the shared UnifiClient for session cookie auth and automatic
re-auth on 401.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

import aiohttp

from oasisagent.clients.unifi import UnifiClient
from oasisagent.handlers.base import Handler
from oasisagent.models import ActionResult, ActionStatus, VerifyResult

if TYPE_CHECKING:
    from oasisagent.config import UnifiHandlerConfig
    from oasisagent.models import Event, RecommendedAction

logger = logging.getLogger(__name__)

_KNOWN_OPERATIONS: frozenset[str] = frozenset({
    "notify",
    "restart_device",
    "block_client",
    "unblock_client",
})


class UnifiHandler(Handler):
    """Executes actions against UniFi Network controller.

    Must be started with ``await handler.start()`` before use.
    The UnifiClient session is created in start() and closed in stop().
    """

    def __init__(self, config: UnifiHandlerConfig) -> None:
        self._config = config
        self._client: UnifiClient | None = None

    def name(self) -> str:
        return "unifi"

    async def start(self) -> None:
        """Create the UniFi client and authenticate."""
        self._client = UnifiClient(
            url=self._config.url,
            username=self._config.username,
            password=self._config.password,
            site=self._config.site,
            is_udm=self._config.is_udm,
            verify_ssl=self._config.verify_ssl,
            timeout=self._config.timeout,
        )
        await self._client.connect()
        logger.info("UniFi handler started (url=%s)", self._config.url)

    async def stop(self) -> None:
        """Close the UniFi client session."""
        if self._client is not None:
            await self._client.close()
            self._client = None
            logger.info("UniFi handler stopped")

    async def can_handle(
        self, event: Event, action: RecommendedAction,
    ) -> bool:
        return (
            action.handler == "unifi"
            and action.operation in _KNOWN_OPERATIONS
        )

    async def execute(
        self, event: Event, action: RecommendedAction,
    ) -> ActionResult:
        """Dispatch to the appropriate operation method."""
        self._ensure_started()

        dispatch = {
            "notify": self._op_notify,
            "restart_device": self._op_restart_device,
            "block_client": self._op_block_client,
            "unblock_client": self._op_unblock_client,
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
                "UniFi handler HTTP error for %s: %s",
                action.operation, exc,
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

        Only restart_device has real verification (polls device state).
        Other operations return verified=True immediately.
        """
        if action.operation != "restart_device":
            return VerifyResult(
                verified=True, message="No verification needed",
            )

        self._ensure_started()
        mac = action.params.get("mac", event.entity_id)
        return await self._verify_device_recovery(mac)

    async def get_context(self, event: Event) -> dict[str, Any]:
        """Gather UniFi-specific context for diagnosis."""
        self._ensure_started()
        assert self._client is not None
        context: dict[str, Any] = {}

        try:
            data = await self._client.get("stat/device")
            devices = data.get("data", [])
            for device in devices:
                if device.get("mac") == event.entity_id:
                    context["device"] = {
                        "name": device.get("name", ""),
                        "type": device.get("type", ""),
                        "model": device.get("model", ""),
                        "version": device.get("version", ""),
                        "state": device.get("state"),
                        "uptime": device.get("uptime"),
                        "system_stats": device.get("system-stats", {}),
                    }
                    break
        except aiohttp.ClientError as exc:
            context["device_error"] = str(exc)

        try:
            data = await self._client.get("stat/health")
            subsystems = data.get("data", [])
            context["health"] = {
                sub.get("subsystem", ""): sub.get("status", "unknown")
                for sub in subsystems
                if sub.get("subsystem")
            }
        except aiohttp.ClientError as exc:
            context["health_error"] = str(exc)

        return context

    # -------------------------------------------------------------------
    # Operation implementations
    # -------------------------------------------------------------------

    async def _op_notify(
        self, event: Event, action: RecommendedAction,
    ) -> ActionResult:
        """Notify — no system changes. Returns the diagnosis message."""
        message = action.params.get("message", action.description)
        logger.info(
            "UniFi notify: %s (entity=%s)", message, event.entity_id,
        )
        return ActionResult(
            status=ActionStatus.SUCCESS,
            details={"message": message, "entity_id": event.entity_id},
        )

    async def _op_restart_device(
        self, event: Event, action: RecommendedAction,
    ) -> ActionResult:
        """Restart a UniFi device via cmd/devmgr."""
        assert self._client is not None
        mac = action.params.get("mac", event.entity_id)
        if not mac:
            return ActionResult(
                status=ActionStatus.FAILURE,
                error_message="restart_device requires 'mac' in params or event.entity_id",
            )

        await self._client.post(
            "cmd/devmgr",
            {"cmd": "restart", "mac": mac},
        )

        logger.info("UniFi restart_device: mac=%s", mac)
        return ActionResult(
            status=ActionStatus.SUCCESS,
            details={"mac": mac, "operation": "restart_device"},
        )

    async def _op_block_client(
        self, event: Event, action: RecommendedAction,
    ) -> ActionResult:
        """Block a client via cmd/stamgr."""
        assert self._client is not None
        mac = action.params.get("mac")
        if not mac:
            return ActionResult(
                status=ActionStatus.FAILURE,
                error_message="block_client requires 'mac' in params",
            )

        await self._client.post(
            "cmd/stamgr",
            {"cmd": "block-sta", "mac": mac},
        )

        logger.info("UniFi block_client: mac=%s", mac)
        return ActionResult(
            status=ActionStatus.SUCCESS,
            details={"mac": mac, "operation": "block_client"},
        )

    async def _op_unblock_client(
        self, event: Event, action: RecommendedAction,
    ) -> ActionResult:
        """Unblock a client via cmd/stamgr."""
        assert self._client is not None
        mac = action.params.get("mac")
        if not mac:
            return ActionResult(
                status=ActionStatus.FAILURE,
                error_message="unblock_client requires 'mac' in params",
            )

        await self._client.post(
            "cmd/stamgr",
            {"cmd": "unblock-sta", "mac": mac},
        )

        logger.info("UniFi unblock_client: mac=%s", mac)
        return ActionResult(
            status=ActionStatus.SUCCESS,
            details={"mac": mac, "operation": "unblock_client"},
        )

    # -------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------

    def _ensure_started(self) -> None:
        """Raise if the handler hasn't been started."""
        if self._client is None:
            msg = "UnifiHandler.start() must be called before use"
            raise RuntimeError(msg)

    async def _verify_device_recovery(self, mac: str) -> VerifyResult:
        """Poll device state to check if it recovers after restart."""
        assert self._client is not None
        timeout = self._config.verify_timeout
        interval = self._config.verify_poll_interval
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            try:
                data = await self._client.get("stat/device")
                devices = data.get("data", [])
                for device in devices:
                    if device.get("mac") == mac:
                        state = device.get("state", 0)
                        if state == 1:
                            name = device.get("name", mac)
                            return VerifyResult(
                                verified=True,
                                message=f"Device {name} ({mac}) recovered (state=1)",
                            )
                        break
            except aiohttp.ClientError:
                pass  # Keep polling

            await asyncio.sleep(interval)

        return VerifyResult(
            verified=False,
            message=(
                f"Device {mac} did not recover within "
                f"{timeout}s (still not state=1)"
            ),
        )
