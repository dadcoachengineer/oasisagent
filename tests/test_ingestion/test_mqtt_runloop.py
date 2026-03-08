"""Run-loop tests for the MQTT ingestion adapter.

Tests start(), reconnect behavior, and graceful shutdown with mocked
aiomqtt client. Focuses on critical paths without over-mocking.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

import aiomqtt

from oasisagent.config import MqttIngestionConfig, MqttTopicMapping
from oasisagent.engine.queue import EventQueue
from oasisagent.ingestion.mqtt import MqttAdapter

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(**overrides: Any) -> MqttIngestionConfig:
    defaults: dict[str, Any] = {
        "enabled": True,
        "broker": "mqtt://localhost:1883",
        "username": "user",
        "password": "pass",
        "client_id": "test-agent",
        "qos": 1,
        "topics": [
            MqttTopicMapping(
                pattern="test/#",
                system="test",
                event_type="test_event",
                severity="info",
            ),
        ],
    }
    defaults.update(overrides)
    return MqttIngestionConfig(**defaults)


def _make_mock_message(
    topic: str = "test/foo",
    payload: dict[str, Any] | None = None,
) -> MagicMock:
    """Create a mock aiomqtt Message."""
    if payload is None:
        payload = {"entity_id": "sensor.test"}
    msg = MagicMock()
    msg.topic = MagicMock()
    msg.topic.__str__ = MagicMock(return_value=topic)
    msg.payload = json.dumps(payload).encode("utf-8")
    return msg


# ---------------------------------------------------------------------------
# start() — one message in, one event out
# ---------------------------------------------------------------------------


class TestMqttStartOneMessage:
    @patch("oasisagent.ingestion.mqtt.aiomqtt.Client")
    async def test_message_produces_event(self, mock_client_cls: MagicMock) -> None:
        """start() connects, subscribes, processes one message, then stops."""
        queue = EventQueue(max_size=10)
        adapter = MqttAdapter(_make_config(), queue)

        message = _make_mock_message(
            topic="test/sensor",
            payload={"entity_id": "sensor.temp", "value": 42},
        )

        # Set up the async context manager and message iterator
        mock_client = AsyncMock()
        mock_client_cls.return_value = mock_client

        # The client is used as `async with Client(...) as client:`
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        # subscribe is awaited
        mock_client.subscribe = AsyncMock()

        # `async for message in client.messages:` — yield one message, then stop
        async def _message_iter() -> AsyncIterator[MagicMock]:
            yield message
            await adapter.stop()

        mock_client.messages = _message_iter()

        await adapter.start()

        assert queue.size == 1
        event = queue.drain()[0]
        assert event.entity_id == "sensor.temp"
        assert event.source == "mqtt"


# ---------------------------------------------------------------------------
# start() — auth failure is fatal (no reconnect)
# ---------------------------------------------------------------------------


class TestMqttAuthFailure:
    @patch("oasisagent.ingestion.mqtt.aiomqtt.Client")
    async def test_auth_failure_stops_adapter(
        self, mock_client_cls: MagicMock
    ) -> None:
        """CONNACK rc=5 (not authorized) should stop without retry."""
        queue = EventQueue(max_size=10)
        adapter = MqttAdapter(_make_config(), queue)

        mock_client = AsyncMock()
        mock_client_cls.return_value = mock_client
        mock_client.__aenter__ = AsyncMock(
            side_effect=aiomqtt.MqttCodeError(5, "not authorized")
        )

        await adapter.start()

        assert await adapter.healthy() is False
        assert adapter._stopping is False  # Not explicitly stopped, just returned


# ---------------------------------------------------------------------------
# start() — connection error triggers reconnect then stop
# ---------------------------------------------------------------------------


class TestMqttReconnect:
    @patch("oasisagent.ingestion.mqtt.aiomqtt.Client")
    async def test_connection_error_retries_then_stops(
        self, mock_client_cls: MagicMock
    ) -> None:
        """MqttError triggers backoff, then we stop the adapter."""
        queue = EventQueue(max_size=10)
        adapter = MqttAdapter(_make_config(), queue)

        call_count = 0

        async def _failing_connect(*args: Any, **kwargs: Any) -> None:
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                await adapter.stop()
            raise aiomqtt.MqttError("connection refused")

        mock_client = AsyncMock()
        mock_client_cls.return_value = mock_client
        mock_client.__aenter__ = _failing_connect

        with patch("oasisagent.ingestion.mqtt.ExponentialBackoff.wait", new_callable=AsyncMock):
            await adapter.start()

        assert call_count >= 2
        assert await adapter.healthy() is False


# ---------------------------------------------------------------------------
# stop() — breaks the message loop
# ---------------------------------------------------------------------------


class TestMqttStop:
    @patch("oasisagent.ingestion.mqtt.aiomqtt.Client")
    async def test_stop_breaks_loop(self, mock_client_cls: MagicMock) -> None:
        """Calling stop() while start() is running should exit cleanly."""
        queue = EventQueue(max_size=10)
        adapter = MqttAdapter(_make_config(), queue)

        mock_client = AsyncMock()
        mock_client_cls.return_value = mock_client
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.subscribe = AsyncMock()

        # Messages iterator that waits forever (simulating idle connection)
        async def _blocking_messages() -> AsyncIterator[MagicMock]:
            while not adapter._stopping:
                await asyncio.sleep(0.01)
            return
            yield  # Make this a generator

        mock_client.messages = _blocking_messages()

        # Start adapter in background, then stop it
        task = asyncio.create_task(adapter.start())
        await asyncio.sleep(0.05)
        await adapter.stop()
        await asyncio.wait_for(task, timeout=2.0)

        assert adapter._stopping is True
