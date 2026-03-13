"""Home Assistant handler — executes actions via the HA REST API.

Phase 1 operations: notify, restart_integration, reload_automations,
call_service, get_entity_state, get_error_log.

ARCHITECTURE.md §8 defines the handler interface and HA-specific operations.
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
    from oasisagent.config import HaHandlerConfig
    from oasisagent.models import Event, RecommendedAction

logger = logging.getLogger(__name__)

# Operations this handler supports. can_handle() whitelists these.
_KNOWN_OPERATIONS: frozenset[str] = frozenset({
    "notify",
    "restart_integration",
    "reload_automations",
    "call_service",
    "get_entity_state",
    "get_error_log",
})


class HandlerNotStartedError(Exception):
    """Raised when handler methods are called before start()."""


class HomeAssistantHandler(Handler):
    """Executes actions against Home Assistant via its REST API.

    Must be started with ``await handler.start()`` before use.
    The aiohttp session is created in start() and closed in stop().
    """

    def __init__(self, config: HaHandlerConfig) -> None:
        self._config = config
        self._session: aiohttp.ClientSession | None = None

    def name(self) -> str:
        return "homeassistant"

    async def start(self) -> None:
        """Create the aiohttp session with bearer token auth."""
        headers: dict[str, str] = {
            "Content-Type": "application/json",
        }
        if self._config.token:
            headers["Authorization"] = f"Bearer {self._config.token}"

        self._session = aiohttp.ClientSession(
            base_url=self._config.url,
            headers=headers,
        )
        logger.info("HA handler started (url=%s)", self._config.url)

    async def stop(self) -> None:
        """Close the aiohttp session."""
        if self._session is not None:
            await self._session.close()
            self._session = None
            logger.info("HA handler stopped")

    async def healthy(self) -> bool:
        """Check HA API connectivity via GET /api/."""
        if self._session is None:
            return False
        async with self._session.get("/api/") as resp:
            return resp.status == 200

    async def can_handle(self, event: Event, action: RecommendedAction) -> bool:
        """Check if this handler supports the action."""
        return (
            action.handler == "homeassistant"
            and action.operation in _KNOWN_OPERATIONS
        )

    async def execute(
        self, event: Event, action: RecommendedAction
    ) -> ActionResult:
        """Dispatch to the appropriate operation method."""
        self._ensure_started()

        dispatch = {
            "notify": self._op_notify,
            "restart_integration": self._op_restart_integration,
            "reload_automations": self._op_reload_automations,
            "call_service": self._op_call_service,
            "get_entity_state": self._op_get_entity_state,
            "get_error_log": self._op_get_error_log,
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
                "HA handler HTTP error for %s: %s", action.operation, exc
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

        Only restart_integration has real verification (polls entity state).
        Other operations return verified=True immediately (Phase 1).
        """
        if action.operation != "restart_integration":
            return VerifyResult(verified=True, message="No verification needed")

        self._ensure_started()
        return await self._verify_entity_recovery(event.entity_id)

    async def get_context(self, event: Event) -> dict[str, Any]:
        """Gather HA-specific context for diagnosis."""
        self._ensure_started()
        context: dict[str, Any] = {}

        try:
            state = await self._get_state(event.entity_id)
            context["entity_state"] = state
        except aiohttp.ClientError as exc:
            context["entity_state_error"] = str(exc)

        try:
            error_log = await self._get_error_log()
            context["recent_errors"] = error_log
        except aiohttp.ClientError as exc:
            context["error_log_error"] = str(exc)

        return context

    # -------------------------------------------------------------------
    # Operation implementations
    # -------------------------------------------------------------------

    async def _op_notify(
        self, event: Event, action: RecommendedAction
    ) -> ActionResult:
        """Notify — no system changes. Returns the diagnosis message."""
        message = action.params.get("message", action.description)
        logger.info("HA notify: %s (entity=%s)", message, event.entity_id)
        return ActionResult(
            status=ActionStatus.SUCCESS,
            details={"message": message, "entity_id": event.entity_id},
        )

    async def _op_restart_integration(
        self, event: Event, action: RecommendedAction
    ) -> ActionResult:
        """Restart an integration via homeassistant.reload_config_entry."""
        entry_id = action.params.get("entry_id")
        if not entry_id:
            return ActionResult(
                status=ActionStatus.FAILURE,
                error_message="restart_integration requires 'entry_id' in params",
            )

        assert self._session is not None
        async with self._session.post(
            "/api/services/homeassistant/reload_config_entry",
            json={"entry_id": entry_id},
        ) as resp:
            resp.raise_for_status()

        logger.info("HA restart_integration: entry_id=%s", entry_id)
        return ActionResult(
            status=ActionStatus.SUCCESS,
            details={"entry_id": entry_id},
        )

    async def _op_reload_automations(
        self, event: Event, action: RecommendedAction
    ) -> ActionResult:
        """Reload all automations."""
        assert self._session is not None
        async with self._session.post(
            "/api/services/automation/reload",
            json={},
        ) as resp:
            resp.raise_for_status()

        logger.info("HA reload_automations completed")
        return ActionResult(
            status=ActionStatus.SUCCESS,
            details={"operation": "reload_automations"},
        )

    async def _op_call_service(
        self, event: Event, action: RecommendedAction
    ) -> ActionResult:
        """Generic HA service call."""
        domain = action.params.get("domain")
        service = action.params.get("service")
        if not domain or not service:
            return ActionResult(
                status=ActionStatus.FAILURE,
                error_message="call_service requires 'domain' and 'service' in params",
            )

        service_data = action.params.get("service_data", {})
        assert self._session is not None
        async with self._session.post(
            f"/api/services/{domain}/{service}",
            json=service_data,
        ) as resp:
            resp.raise_for_status()

        logger.info("HA call_service: %s.%s", domain, service)
        return ActionResult(
            status=ActionStatus.SUCCESS,
            details={"domain": domain, "service": service, "service_data": service_data},
        )

    async def _op_get_entity_state(
        self, event: Event, action: RecommendedAction
    ) -> ActionResult:
        """Read entity state — context-gathering, not a mutating action."""
        entity_id = action.params.get("entity_id", event.entity_id)
        state = await self._get_state(entity_id)
        return ActionResult(
            status=ActionStatus.SUCCESS,
            details={"entity_id": entity_id, "state": state},
        )

    async def _op_get_error_log(
        self, event: Event, action: RecommendedAction
    ) -> ActionResult:
        """Fetch recent error log entries."""
        log_text = await self._get_error_log()
        return ActionResult(
            status=ActionStatus.SUCCESS,
            details={"error_log": log_text},
        )

    # -------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------

    def _ensure_started(self) -> None:
        """Raise if the handler hasn't been started."""
        if self._session is None:
            raise HandlerNotStartedError(
                "HomeAssistantHandler.start() must be called before use"
            )

    async def _get_state(self, entity_id: str) -> dict[str, Any]:
        """GET /api/states/{entity_id}."""
        assert self._session is not None
        async with self._session.get(f"/api/states/{entity_id}") as resp:
            resp.raise_for_status()
            return await resp.json()

    async def _get_error_log(self) -> str:
        """GET /api/error_log — returns plain text."""
        assert self._session is not None
        async with self._session.get("/api/error_log") as resp:
            resp.raise_for_status()
            return await resp.text()

    async def _verify_entity_recovery(self, entity_id: str) -> VerifyResult:
        """Poll entity state to check if it recovers from unavailable."""
        timeout = self._config.verify_timeout
        interval = self._config.verify_poll_interval
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            try:
                state = await self._get_state(entity_id)
                current = state.get("state", "unknown")
                if current not in ("unavailable", "unknown"):
                    return VerifyResult(
                        verified=True,
                        message=f"Entity {entity_id} recovered to state '{current}'",
                    )
            except aiohttp.ClientError:
                pass  # Keep polling

            await asyncio.sleep(interval)

        return VerifyResult(
            verified=False,
            message=(
                f"Entity {entity_id} did not recover within "
                f"{timeout}s (still unavailable/unknown)"
            ),
        )
