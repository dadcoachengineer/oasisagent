"""MQTT ingestion adapter.

Connects to an MQTT broker, subscribes to configured topic patterns,
and transforms messages into canonical Event objects.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import aiomqtt

from oasisagent.backoff import ExponentialBackoff
from oasisagent.ingestion.base import IngestAdapter
from oasisagent.models import SEVERITY_MAP, Event, EventMetadata, Severity

if TYPE_CHECKING:
    from oasisagent.config import MqttIngestionConfig, MqttTopicMapping
    from oasisagent.engine.queue import EventQueue

logger = logging.getLogger(__name__)

_DEFAULT_SEVERITY = Severity.WARNING
_DEFAULT_SYSTEM = "unknown"
_DEFAULT_EVENT_TYPE = "unknown"


class MqttAdapter(IngestAdapter):
    """Ingestion adapter that subscribes to MQTT topics.

    Connects to the configured MQTT broker, subscribes to topic patterns
    defined in config, and transforms incoming JSON messages into Events.

    Non-JSON messages are logged and skipped. Connection failures trigger
    exponential backoff reconnection. Authentication failures are treated
    as fatal configuration errors.
    """

    def __init__(self, config: MqttIngestionConfig, queue: EventQueue) -> None:
        super().__init__(queue)
        self._config = config
        self._connected = False
        self._stopping = False
        self._backoff = ExponentialBackoff(name="mqtt")

        parsed = urlparse(config.broker)
        self._hostname = parsed.hostname or "localhost"
        self._port = parsed.port or 1883

    @property
    def name(self) -> str:
        return "mqtt"

    async def start(self) -> None:
        """Connect to MQTT broker and process messages in a loop."""
        while not self._stopping:
            try:
                await self._connect_and_listen()
            except aiomqtt.MqttCodeError as exc:
                if self._stopping:
                    break
                # Auth failures are fatal — don't retry
                if exc.rc in (4, 5):  # CONNACK: bad credentials / not authorized
                    logger.error(
                        "MQTT authentication failed (rc=%d). "
                        "Check MQTT_USER and MQTT_PASS in your .env file.",
                        exc.rc,
                    )
                    self._connected = False
                    return
                logger.error("MQTT connection error: %s", exc)
                self._connected = False
                await self._backoff.wait()
            except aiomqtt.MqttError as exc:
                if self._stopping:
                    break
                logger.error("MQTT error: %s", exc)
                self._connected = False
                await self._backoff.wait()
            except Exception:
                if self._stopping:
                    break
                logger.exception("Unexpected error in MQTT adapter")
                self._connected = False
                await self._backoff.wait()

    async def stop(self) -> None:
        self._stopping = True
        self._connected = False

    async def healthy(self) -> bool:
        return self._connected

    async def _connect_and_listen(self) -> None:
        """Establish connection, subscribe, and process messages."""
        async with aiomqtt.Client(
            hostname=self._hostname,
            port=self._port,
            username=self._config.username or None,
            password=self._config.password or None,
            identifier=self._config.client_id,
        ) as client:
            self._connected = True
            self._backoff.reset()
            logger.info(
                "MQTT connected to %s:%d as %s",
                self._hostname,
                self._port,
                self._config.client_id,
            )

            for topic_mapping in self._config.topics:
                await client.subscribe(
                    topic_mapping.pattern,
                    qos=self._config.qos,
                )
                logger.info("MQTT subscribed to %s", topic_mapping.pattern)

            async for message in client.messages:
                if self._stopping:
                    break
                self._handle_message(message)

    def _handle_message(self, message: aiomqtt.Message) -> None:
        """Transform an MQTT message into an Event and enqueue it."""
        topic = str(message.topic)
        raw_payload = message.payload

        if isinstance(raw_payload, (bytes, bytearray)):
            try:
                payload_str = raw_payload.decode("utf-8")
            except UnicodeDecodeError:
                logger.warning("MQTT: non-UTF8 payload on topic %s, skipping", topic)
                return
        elif isinstance(raw_payload, str):
            payload_str = raw_payload
        else:
            logger.warning(
                "MQTT: unexpected payload type %s on topic %s, skipping",
                type(raw_payload),
                topic,
            )
            return

        try:
            payload: dict[str, Any] = json.loads(payload_str)
        except json.JSONDecodeError:
            logger.warning("MQTT: non-JSON payload on topic %s, skipping", topic)
            return

        if not isinstance(payload, dict):
            logger.warning("MQTT: payload is not a JSON object on topic %s, skipping", topic)
            return

        mapping = self._find_mapping(topic)
        system = self._resolve_field(mapping.system if mapping else "auto", payload, "system")
        event_type = self._resolve_field(
            mapping.event_type if mapping else "auto", payload, "event_type"
        )
        severity = self._resolve_severity(mapping.severity if mapping else "auto", payload)
        entity_id = payload.get("entity_id", topic)

        event = Event(
            source=self.name,
            system=system,
            event_type=event_type,
            entity_id=entity_id,
            severity=severity,
            timestamp=datetime.now(UTC),
            payload=payload,
            metadata=EventMetadata(
                dedup_key=f"mqtt:{entity_id}:{event_type}",
            ),
        )

        try:
            self._queue.put_nowait(event)
        except Exception:
            logger.warning("MQTT: failed to enqueue event from topic %s", topic)

    def _find_mapping(self, topic: str) -> MqttTopicMapping | None:
        """Find the first topic mapping that matches the given topic."""
        for mapping in self._config.topics:
            if _topic_matches(mapping.pattern, topic):
                return mapping
        return None

    @staticmethod
    def _resolve_field(
        configured: str,
        payload: dict[str, Any],
        field_name: str,
    ) -> str:
        """Resolve a field value from config or payload."""
        if configured != "auto":
            return configured
        default = _DEFAULT_SYSTEM if field_name == "system" else _DEFAULT_EVENT_TYPE
        return str(payload.get(field_name, default))

    @staticmethod
    def _resolve_severity(configured: str, payload: dict[str, Any]) -> Severity:
        """Resolve severity from config, payload, or default."""
        if configured != "auto":
            return SEVERITY_MAP.get(configured, _DEFAULT_SEVERITY)
        payload_severity = str(payload.get("severity", ""))
        return SEVERITY_MAP.get(payload_severity, _DEFAULT_SEVERITY)


def _topic_matches(pattern: str, topic: str) -> bool:
    """Check if an MQTT topic matches a subscription pattern.

    Supports MQTT wildcards:
    - '#' matches any number of levels (must be last)
    - '+' matches exactly one level
    """
    pattern_parts = pattern.split("/")
    topic_parts = topic.split("/")

    for i, p in enumerate(pattern_parts):
        if p == "#":
            return True
        if i >= len(topic_parts):
            return False
        if p == "+":
            continue
        if p != topic_parts[i]:
            return False

    return len(pattern_parts) == len(topic_parts)
