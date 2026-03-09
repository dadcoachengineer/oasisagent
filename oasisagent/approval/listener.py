"""Approval listener — subscribes to MQTT approval/rejection topics.

Connects to the MQTT broker and listens for operator decisions on pending
actions. Follows the same lifecycle pattern as IngestAdapter — the
orchestrator launches it as a background task.

ARCHITECTURE.md §16.2 describes the MQTT topic contract.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import aiomqtt

from oasisagent.backoff import ExponentialBackoff

if TYPE_CHECKING:
    from collections.abc import Callable

    from oasisagent.config import MqttIngestionConfig

logger = logging.getLogger(__name__)

# MQTT topic patterns for approval/rejection
_APPROVE_TOPIC = "oasis/approve/+"
_REJECT_TOPIC = "oasis/reject/+"


class ApprovalListener:
    """Subscribes to MQTT approval/rejection topics and routes to callbacks.

    Args:
        config: MQTT broker config (reuses ingestion MQTT config for
            broker address and credentials).
        on_approve: Called with action_id when an approval message arrives.
        on_reject: Called with action_id when a rejection message arrives.
    """

    def __init__(
        self,
        config: MqttIngestionConfig,
        on_approve: Callable[[str], None],
        on_reject: Callable[[str], None],
    ) -> None:
        self._config = config
        self._on_approve = on_approve
        self._on_reject = on_reject
        self._stopping = False
        self._connected = False
        self._backoff = ExponentialBackoff(name="approval-listener")

        parsed = urlparse(config.broker)
        self._hostname = parsed.hostname or "localhost"
        self._port = parsed.port or 1883

    async def start(self) -> None:
        """Connect to MQTT and listen for approval/rejection messages.

        Runs in a reconnect loop until stop() is called.
        """
        while not self._stopping:
            try:
                await self._connect_and_listen()
            except aiomqtt.MqttCodeError as exc:
                if self._stopping:
                    break
                if exc.rc in (4, 5):
                    logger.error(
                        "Approval listener: MQTT auth failed (rc=%d)",
                        exc.rc,
                    )
                    self._connected = False
                    return
                logger.error("Approval listener: MQTT error: %s", exc)
                self._connected = False
                await self._backoff.wait()
            except aiomqtt.MqttError as exc:
                if self._stopping:
                    break
                logger.error("Approval listener: MQTT error: %s", exc)
                self._connected = False
                await self._backoff.wait()
            except Exception:
                if self._stopping:
                    break
                logger.exception("Approval listener: unexpected error")
                self._connected = False
                await self._backoff.wait()

    async def stop(self) -> None:
        """Signal the listener to stop."""
        self._stopping = True
        self._connected = False

    @property
    def connected(self) -> bool:
        """Whether the listener currently has an active MQTT connection."""
        return self._connected

    async def _connect_and_listen(self) -> None:
        """Establish connection, subscribe, and process messages."""
        async with aiomqtt.Client(
            hostname=self._hostname,
            port=self._port,
            username=self._config.username or None,
            password=self._config.password or None,
            identifier=f"{self._config.client_id}-approval",
        ) as client:
            self._connected = True
            self._backoff.reset()
            logger.info(
                "Approval listener connected to %s:%d",
                self._hostname,
                self._port,
            )

            await client.subscribe(_APPROVE_TOPIC, qos=1)
            await client.subscribe(_REJECT_TOPIC, qos=1)
            logger.info(
                "Approval listener subscribed to %s and %s",
                _APPROVE_TOPIC,
                _REJECT_TOPIC,
            )

            async for message in client.messages:
                if self._stopping:
                    break
                self._handle_message(message)

    def _handle_message(self, message: aiomqtt.Message) -> None:
        """Route an MQTT message to the appropriate callback."""
        topic = str(message.topic)
        parts = topic.split("/")

        # Expected: oasis/approve/{action_id} or oasis/reject/{action_id}
        if len(parts) != 3 or parts[0] != "oasis":
            logger.warning(
                "Approval listener: unexpected topic structure: %s",
                topic,
            )
            return

        action_id = parts[2]
        action_type = parts[1]

        if action_type == "approve":
            logger.info("Approval received for action %s", action_id)
            self._on_approve(action_id)
        elif action_type == "reject":
            logger.info("Rejection received for action %s", action_id)
            self._on_reject(action_id)
        else:
            logger.warning(
                "Approval listener: unknown action type '%s' in topic %s",
                action_type,
                topic,
            )
