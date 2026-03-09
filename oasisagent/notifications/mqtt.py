"""MQTT notification channel — publishes notifications to MQTT topics.

Topic structure: {topic_prefix}/{severity}
Example: oasis/notifications/error, oasis/notifications/warning

This allows downstream consumers (HA automations, Node-RED) to subscribe
to specific severity levels or use wildcard oasis/notifications/#.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import aiomqtt

from oasisagent.notifications.base import NotificationChannel

if TYPE_CHECKING:
    from oasisagent.config import MqttNotificationConfig
    from oasisagent.models import Notification

logger = logging.getLogger(__name__)


class MqttNotificationChannel(NotificationChannel):
    """Publishes notifications as JSON to MQTT topics.

    Must be started with ``await channel.start()`` before use.
    If disabled in config, ``send()`` returns True immediately (no-op).
    """

    def __init__(self, config: MqttNotificationConfig) -> None:
        self._config = config
        self._client: aiomqtt.Client | None = None

    def name(self) -> str:
        return "mqtt"

    async def start(self) -> None:
        """Connect to the MQTT broker."""
        if not self._config.enabled:
            logger.info("MQTT notifications disabled — skipping connection")
            return

        parsed = urlparse(self._config.broker)
        hostname = parsed.hostname or "localhost"
        port = parsed.port or 1883

        self._client = aiomqtt.Client(
            hostname=hostname,
            port=port,
            username=self._config.username or None,
            password=self._config.password or None,
        )
        await self._client.__aenter__()
        logger.info(
            "MQTT notification channel started (broker=%s, prefix=%s)",
            self._config.broker,
            self._config.topic_prefix,
        )

    async def stop(self) -> None:
        """Disconnect from the MQTT broker."""
        if self._client is not None:
            try:
                await self._client.__aexit__(None, None, None)
            except Exception as exc:
                logger.warning("Error closing MQTT notification client: %s", exc)
            self._client = None
            logger.info("MQTT notification channel stopped")

    async def publish_raw(
        self,
        topic: str,
        payload: str | bytes,
        *,
        qos: int = 1,
        retain: bool = False,
    ) -> bool:
        """Publish a raw message to an arbitrary MQTT topic.

        Used by the orchestrator for approval queue topics
        (oasis/pending/*, oasis/pending/list) that are not standard
        notifications.

        Returns True on success, False on failure (best-effort).
        """
        if self._client is None:
            logger.warning(
                "MQTT channel not started — cannot publish to %s", topic
            )
            return False

        try:
            await self._client.publish(
                topic=topic,
                payload=payload,
                qos=qos,
                retain=retain,
            )
            logger.debug("MQTT raw publish: topic=%s, retain=%s", topic, retain)
            return True
        except Exception as exc:
            logger.warning("MQTT raw publish failed for %s: %s", topic, exc)
            return False

    async def send(self, notification: Notification) -> bool:
        """Publish a notification to MQTT.

        Topic: {topic_prefix}/{severity}
        Payload: JSON-serialized Notification model.

        Returns True on success, False on failure (best-effort).
        """
        if not self._config.enabled:
            return True

        if self._client is None:
            logger.warning(
                "MQTT notification channel not started — dropping notification %s",
                notification.id,
            )
            return False

        topic = f"{self._config.topic_prefix}/{notification.severity.value}"
        payload = notification.model_dump_json()

        try:
            await self._client.publish(
                topic=topic,
                payload=payload,
                qos=self._config.qos,
                retain=self._config.retain,
            )
            logger.debug(
                "MQTT notification sent: topic=%s, id=%s",
                topic,
                notification.id,
            )
            return True
        except Exception as exc:
            logger.warning(
                "MQTT notification publish failed for %s: %s",
                notification.id,
                exc,
            )
            return False
