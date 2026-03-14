"""Home Assistant WebSocket ingestion adapter.

Connects to HA's WebSocket API, subscribes to all events, and filters
for state_changed, automation_triggered, and call_service. Transforms
relevant events into canonical Event objects.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import aiohttp

from oasisagent.backoff import ExponentialBackoff
from oasisagent.ingestion.base import IngestAdapter
from oasisagent.models import Event, EventMetadata, Severity

if TYPE_CHECKING:
    from oasisagent.config import HaWebSocketConfig
    from oasisagent.engine.queue import EventQueue

logger = logging.getLogger(__name__)


class HaWebSocketAdapter(IngestAdapter):
    """Ingestion adapter for Home Assistant's WebSocket API.

    Connects to HA, authenticates with a long-lived access token,
    subscribes to events, and filters/transforms them into canonical Events.

    Connection failures trigger exponential backoff reconnection.
    Authentication failures are treated as fatal configuration errors.
    """

    def __init__(self, config: HaWebSocketConfig, queue: EventQueue) -> None:
        super().__init__(queue)
        self._config = config
        self._connected = False
        self._stopping = False
        self._backoff = ExponentialBackoff(name="ha_websocket")
        self._msg_id = 0

    @property
    def name(self) -> str:
        return "ha_websocket"

    async def start(self) -> None:
        """Connect to HA WebSocket and process events in a loop."""
        while not self._stopping:
            try:
                await self._connect_and_listen()
            except aiohttp.WSServerHandshakeError as exc:
                if self._stopping:
                    break
                logger.error(
                    "HA WebSocket handshake failed (status=%s). "
                    "Check the URL in config: %s",
                    exc.status,
                    self._config.url,
                )
                self._connected = False
                await self._backoff.wait()
            except (TimeoutError, aiohttp.ClientError) as exc:
                if self._stopping:
                    break
                logger.error("HA WebSocket connection error: %s", exc)
                self._connected = False
                await self._backoff.wait()
            except Exception:
                if self._stopping:
                    break
                logger.exception("Unexpected error in HA WebSocket adapter")
                self._connected = False
                await self._backoff.wait()

    async def stop(self) -> None:
        self._stopping = True
        self._connected = False

    async def healthy(self) -> bool:
        return self._connected

    def _next_id(self) -> int:
        """Generate the next message ID for the WebSocket protocol."""
        self._msg_id += 1
        return self._msg_id

    async def _connect_and_listen(self) -> None:
        """Establish connection, authenticate, subscribe, and process."""
        async with (
            aiohttp.ClientSession() as session,
            session.ws_connect(self._config.url) as ws,
        ):
            # Step 1: Receive auth_required
            auth_required = await ws.receive_json()
            if auth_required.get("type") != "auth_required":
                logger.error(
                    "HA WebSocket: expected auth_required, got %s",
                    auth_required.get("type"),
                )
                return

            # Step 2: Send auth
            await ws.send_json({
                "type": "auth",
                "access_token": self._config.token,
            })

            # Step 3: Receive auth result
            auth_result = await ws.receive_json()
            if auth_result.get("type") == "auth_invalid":
                logger.error(
                    "HA WebSocket authentication failed: %s. "
                    "Check HA_TOKEN in your .env file.",
                    auth_result.get("message", "invalid token"),
                )
                self._stopping = True
                self._connected = False
                return

            if auth_result.get("type") != "auth_ok":
                logger.error(
                    "HA WebSocket: expected auth_ok, got %s",
                    auth_result.get("type"),
                )
                return

            self._connected = True
            self._backoff.reset()
            logger.info("HA WebSocket connected and authenticated")

            # Step 4: Subscribe to events
            sub_id = self._next_id()
            await ws.send_json({
                "id": sub_id,
                "type": "subscribe_events",
            })

            # Step 5: Process events
            async for msg in ws:
                if self._stopping:
                    break
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                    except json.JSONDecodeError:
                        logger.warning("HA WebSocket: non-JSON message, skipping")
                        continue
                    self._handle_event(data)
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    logger.warning("HA WebSocket connection closed")
                    self._connected = False
                    break

    def _handle_event(self, data: dict[str, Any]) -> None:
        """Route an HA event to the appropriate handler."""
        if data.get("type") != "event":
            return

        event_data = data.get("event", {})
        event_type = event_data.get("event_type", "")

        if event_type == "state_changed":
            self._handle_state_changed(event_data)
        elif event_type == "automation_triggered":
            self._handle_automation_triggered(event_data)
        elif event_type == "call_service":
            self._handle_service_call(event_data)

    def _handle_state_changed(self, event_data: dict[str, Any]) -> None:
        """Process state_changed events — filter for bad state transitions."""
        if not self._config.subscriptions.state_changes.enabled:
            return

        data = event_data.get("data", {})
        new_state = data.get("new_state", {})
        entity_id = new_state.get("entity_id", "")
        state = new_state.get("state", "")

        trigger_states = self._config.subscriptions.state_changes.trigger_states
        if state not in trigger_states:
            return

        ignore_entities = self._config.subscriptions.state_changes.ignore_entities
        if entity_id in ignore_entities:
            return

        min_duration = self._config.subscriptions.state_changes.min_duration
        if min_duration > 0:
            logger.debug(
                "min_duration=%d is configured but not yet implemented. "
                "Emitting state_changed event for %s immediately.",
                min_duration,
                entity_id,
            )

        old_state = data.get("old_state", {})
        event = Event(
            source=self.name,
            system="homeassistant",
            event_type="state_unavailable",
            entity_id=entity_id,
            severity=Severity.WARNING,
            timestamp=datetime.now(UTC),
            payload={
                "old_state": old_state.get("state", ""),
                "new_state": state,
                "attributes": new_state.get("attributes", {}),
            },
            metadata=EventMetadata(
                dedup_key=f"ha_ws:{entity_id}:state_unavailable",
            ),
        )

        try:
            self._queue.put_nowait(event)
        except Exception:
            logger.warning("HA WebSocket: failed to enqueue state_changed event for %s", entity_id)

    def _handle_automation_triggered(self, event_data: dict[str, Any]) -> None:
        """Process automation_triggered events — filter for failures."""
        if not self._config.subscriptions.automation_failures.enabled:
            return

        data = event_data.get("data", {})
        entity_id = data.get("entity_id", "")

        # HA reports automation results; we only care about failures
        # The "result" field may not exist for successful triggers in some HA versions
        context = event_data.get("context", {})
        # Check for error indicators in the event data
        source = data.get("source", "")
        if source == "error" or data.get("error"):
            error_msg = data.get("error", "Automation triggered with errors")
        else:
            # Not a failure — skip
            return

        event = Event(
            source=self.name,
            system="homeassistant",
            event_type="automation_error",
            entity_id=entity_id,
            severity=Severity.ERROR,
            timestamp=datetime.now(UTC),
            payload={
                "error": error_msg,
                "automation_data": data,
                "context": context,
            },
            metadata=EventMetadata(
                dedup_key=f"ha_ws:{entity_id}:automation_error",
            ),
        )

        try:
            self._queue.put_nowait(event)
        except Exception:
            logger.warning(
                "HA WebSocket: failed to enqueue automation_error for %s", entity_id
            )

    def _handle_service_call(self, event_data: dict[str, Any]) -> None:
        """Process call_service events — capture error responses."""
        if not self._config.subscriptions.service_call_errors.enabled:
            return

        data = event_data.get("data", {})
        # Service call events that indicate errors
        if not data.get("error"):
            return

        domain = data.get("domain", "")
        service = data.get("service", "")
        entity_id = f"{domain}.{service}"
        error_msg = data.get("error", "Service call failed")

        event = Event(
            source=self.name,
            system="homeassistant",
            event_type="service_call_failure",
            entity_id=entity_id,
            severity=Severity.ERROR,
            timestamp=datetime.now(UTC),
            payload={
                "domain": domain,
                "service": service,
                "error": error_msg,
                "service_data": data.get("service_data", {}),
            },
            metadata=EventMetadata(
                dedup_key=f"ha_ws:{entity_id}:service_call_failure",
            ),
        )

        try:
            self._queue.put_nowait(event)
        except Exception:
            logger.warning(
                "HA WebSocket: failed to enqueue service_call_failure for %s", entity_id
            )
