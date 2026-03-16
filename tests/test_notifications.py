"""Tests for notification channels and dispatcher."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from oasisagent.config import MqttNotificationConfig
from oasisagent.models import Notification, Severity
from oasisagent.notifications.base import NotificationChannel
from oasisagent.notifications.dispatcher import NotificationDispatcher
from oasisagent.notifications.mqtt import MqttNotificationChannel

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(**overrides: Any) -> MqttNotificationConfig:
    defaults: dict[str, Any] = {
        "enabled": True,
        "broker": "mqtt://localhost:1883",
        "topic_prefix": "oasis/notifications",
        "username": "user",
        "password": "pass",
        "qos": 1,
        "retain": False,
    }
    defaults.update(overrides)
    return MqttNotificationConfig(**defaults)


def _make_notification(**overrides: Any) -> Notification:
    defaults: dict[str, Any] = {
        "title": "Test Alert",
        "message": "Something happened",
        "severity": Severity.WARNING,
        "event_id": "evt-001",
    }
    defaults.update(overrides)
    return Notification(**defaults)


def _mock_mqtt_channel(
    send_result: bool = True,
) -> MqttNotificationChannel:
    """Create an MQTT channel with a mocked client."""
    channel = MqttNotificationChannel(_make_config())
    mock_client = AsyncMock()
    if not send_result:
        mock_client.publish.side_effect = Exception("publish failed")
    channel._client = mock_client
    return channel


class _StubChannel(NotificationChannel):
    """Test stub implementing NotificationChannel."""

    def __init__(self, channel_name: str = "stub", success: bool = True) -> None:
        self._name = channel_name
        self._success = success
        self.sent: list[Notification] = []

    def name(self) -> str:
        return self._name

    async def send(self, notification: Notification) -> bool:
        self.sent.append(notification)
        return self._success


class _FailingChannel(NotificationChannel):
    """Channel that raises on start/send/stop."""

    def __init__(self, fail_on: str = "send") -> None:
        self._fail_on = fail_on

    def name(self) -> str:
        return "failing"

    async def start(self) -> None:
        if self._fail_on == "start":
            raise ConnectionError("cannot connect")

    async def stop(self) -> None:
        if self._fail_on == "stop":
            raise ConnectionError("cannot disconnect")

    async def send(self, notification: Notification) -> bool:
        if self._fail_on == "send":
            raise ConnectionError("cannot send")
        return True


# ---------------------------------------------------------------------------
# MQTT channel: name
# ---------------------------------------------------------------------------


class TestMqttName:
    def test_name_is_mqtt(self) -> None:
        channel = MqttNotificationChannel(_make_config())
        assert channel.name() == "mqtt"


# ---------------------------------------------------------------------------
# MQTT channel: topic structure
# ---------------------------------------------------------------------------


class TestMqttTopicStructure:
    """Notifications publish to {prefix}/{severity}."""

    async def test_error_severity_topic(self) -> None:
        channel = _mock_mqtt_channel()
        notification = _make_notification(severity=Severity.ERROR)

        await channel.send(notification)

        channel._client.publish.assert_called_once()
        call_kwargs = channel._client.publish.call_args.kwargs
        assert call_kwargs["topic"] == "oasis/notifications/error"

    async def test_warning_severity_topic(self) -> None:
        channel = _mock_mqtt_channel()
        notification = _make_notification(severity=Severity.WARNING)

        await channel.send(notification)

        call_kwargs = channel._client.publish.call_args.kwargs
        assert call_kwargs["topic"] == "oasis/notifications/warning"

    async def test_info_severity_topic(self) -> None:
        channel = _mock_mqtt_channel()
        notification = _make_notification(severity=Severity.INFO)

        await channel.send(notification)

        call_kwargs = channel._client.publish.call_args.kwargs
        assert call_kwargs["topic"] == "oasis/notifications/info"

    async def test_critical_severity_topic(self) -> None:
        channel = _mock_mqtt_channel()
        notification = _make_notification(severity=Severity.CRITICAL)

        await channel.send(notification)

        call_kwargs = channel._client.publish.call_args.kwargs
        assert call_kwargs["topic"] == "oasis/notifications/critical"

    async def test_custom_prefix(self) -> None:
        channel = MqttNotificationChannel(_make_config(topic_prefix="alerts"))
        channel._client = AsyncMock()
        notification = _make_notification(severity=Severity.ERROR)

        await channel.send(notification)

        call_kwargs = channel._client.publish.call_args.kwargs
        assert call_kwargs["topic"] == "alerts/error"


# ---------------------------------------------------------------------------
# MQTT channel: payload
# ---------------------------------------------------------------------------


class TestMqttPayload:
    async def test_payload_is_json(self) -> None:
        channel = _mock_mqtt_channel()
        notification = _make_notification(title="Test", message="Hello")

        await channel.send(notification)

        call_kwargs = channel._client.publish.call_args.kwargs
        payload = call_kwargs["payload"]
        import json

        data = json.loads(payload)
        assert data["title"] == "Test"
        assert data["message"] == "Hello"
        assert data["severity"] == "warning"
        assert data["event_id"] == "evt-001"


# ---------------------------------------------------------------------------
# MQTT channel: QoS and retain
# ---------------------------------------------------------------------------


class TestMqttQosRetain:
    async def test_default_qos_1(self) -> None:
        channel = _mock_mqtt_channel()

        await channel.send(_make_notification())

        call_kwargs = channel._client.publish.call_args.kwargs
        assert call_kwargs["qos"] == 1

    async def test_custom_qos(self) -> None:
        channel = MqttNotificationChannel(_make_config(qos=2))
        channel._client = AsyncMock()

        await channel.send(_make_notification())

        call_kwargs = channel._client.publish.call_args.kwargs
        assert call_kwargs["qos"] == 2

    async def test_default_retain_false(self) -> None:
        channel = _mock_mqtt_channel()

        await channel.send(_make_notification())

        call_kwargs = channel._client.publish.call_args.kwargs
        assert call_kwargs["retain"] is False

    async def test_retain_true(self) -> None:
        channel = MqttNotificationChannel(_make_config(retain=True))
        channel._client = AsyncMock()

        await channel.send(_make_notification())

        call_kwargs = channel._client.publish.call_args.kwargs
        assert call_kwargs["retain"] is True


# ---------------------------------------------------------------------------
# MQTT channel: disabled mode
# ---------------------------------------------------------------------------


class TestMqttDisabled:
    async def test_send_returns_true(self) -> None:
        channel = MqttNotificationChannel(_make_config(enabled=False))

        result = await channel.send(_make_notification())

        assert result is True

    async def test_start_skips_connection(self) -> None:
        channel = MqttNotificationChannel(_make_config(enabled=False))

        await channel.start()

        assert channel._client is None


# ---------------------------------------------------------------------------
# MQTT channel: error handling
# ---------------------------------------------------------------------------


class TestMqttErrors:
    async def test_publish_failure_returns_false(self) -> None:
        channel = _mock_mqtt_channel()
        channel._client.publish.side_effect = Exception("broker down")

        result = await channel.send(_make_notification())

        assert result is False

    async def test_send_without_start_returns_false(self) -> None:
        channel = MqttNotificationChannel(_make_config())

        result = await channel.send(_make_notification())

        assert result is False

    async def test_send_failure_triggers_reconnect(self) -> None:
        """Mid-session publish failure should spawn a reconnect task."""
        channel = _mock_mqtt_channel()
        channel._client.publish.side_effect = Exception("broker dropped")

        result = await channel.send(_make_notification())

        assert result is False
        assert channel._client is None
        assert channel._reconnect_task is not None
        assert not channel._reconnect_task.done()

        await channel.stop()

    async def test_publish_raw_failure_triggers_reconnect(self) -> None:
        """Mid-session raw publish failure should spawn a reconnect task."""
        channel = _mock_mqtt_channel()
        channel._client.publish.side_effect = Exception("broker dropped")

        result = await channel.publish_raw("topic", "payload")

        assert result is False
        assert channel._client is None
        assert channel._reconnect_task is not None

        await channel.stop()

    async def test_trigger_reconnect_calls_aexit_on_stale_client(self) -> None:
        """_trigger_reconnect() should schedule __aexit__ on the old client."""
        channel = _mock_mqtt_channel()
        old_client = channel._client
        old_client.__aexit__ = AsyncMock()

        channel._trigger_reconnect()

        # Let the fire-and-forget cleanup task run
        await asyncio.sleep(0)

        old_client.__aexit__.assert_awaited_once_with(None, None, None)
        assert channel._client is None

        await channel.stop()

    async def test_trigger_reconnect_suppresses_aexit_error(self) -> None:
        """If __aexit__ raises on the stale client, it should be suppressed."""
        channel = _mock_mqtt_channel()
        old_client = channel._client
        old_client.__aexit__ = AsyncMock(side_effect=OSError("socket gone"))

        channel._trigger_reconnect()

        # Let the fire-and-forget cleanup task run — should not raise
        await asyncio.sleep(0)

        old_client.__aexit__.assert_awaited_once_with(None, None, None)

        await channel.stop()

    async def test_trigger_reconnect_skips_cleanup_when_no_client(self) -> None:
        """If _client is already None, no cleanup task is created."""
        channel = MqttNotificationChannel(_make_config())
        assert channel._client is None

        channel._trigger_reconnect()

        # Only the reconnect task should exist, no stale cleanup
        assert channel._reconnect_task is not None

        await channel.stop()

    async def test_duplicate_reconnect_not_spawned(self) -> None:
        """Multiple publish failures should not spawn duplicate reconnect tasks."""
        channel = _mock_mqtt_channel()
        channel._client.publish.side_effect = Exception("broker dropped")

        await channel.send(_make_notification())
        first_task = channel._reconnect_task

        # Second failure — client is None so send returns False early
        # without spawning another task
        await channel.send(_make_notification())

        assert channel._reconnect_task is first_task

        await channel.stop()


# ---------------------------------------------------------------------------
# MQTT channel: lifecycle
# ---------------------------------------------------------------------------


class TestMqttLifecycle:
    @patch("oasisagent.notifications.mqtt.aiomqtt.Client")
    async def test_start_connects(self, mock_cls: MagicMock) -> None:
        mock_instance = AsyncMock()
        mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
        mock_cls.return_value = mock_instance

        channel = MqttNotificationChannel(_make_config())
        await channel.start()

        mock_cls.assert_called_once()
        assert channel._client is not None

    @patch("oasisagent.notifications.mqtt.aiomqtt.Client")
    async def test_start_does_not_block_on_failure(
        self, mock_cls: MagicMock,
    ) -> None:
        """If initial connection fails, start() returns immediately
        and spawns a background reconnect task."""
        mock_instance = AsyncMock()
        mock_instance.__aenter__ = AsyncMock(
            side_effect=ConnectionRefusedError("Connection refused"),
        )
        mock_cls.return_value = mock_instance

        channel = MqttNotificationChannel(_make_config())
        await channel.start()

        # start() returned without blocking
        assert channel._client is None
        assert channel._reconnect_task is not None
        assert not channel._reconnect_task.done()

        # Clean up
        await channel.stop()
        assert channel._reconnect_task is None

    async def test_stop_disconnects(self) -> None:
        channel = _mock_mqtt_channel()
        channel._client.__aexit__ = AsyncMock()

        await channel.stop()

        assert channel._client is None

    async def test_stop_without_start_is_noop(self) -> None:
        channel = MqttNotificationChannel(_make_config())
        await channel.stop()  # Should not raise

    async def test_stop_cancels_reconnect_task(self) -> None:
        """Stop cancels any pending background reconnect."""
        channel = MqttNotificationChannel(_make_config())
        # Simulate a reconnect task in progress
        channel._reconnect_task = asyncio.create_task(asyncio.sleep(9999))

        await channel.stop()

        assert channel._reconnect_task is None

    @patch("oasisagent.notifications.mqtt.aiomqtt.Client")
    async def test_connect_does_not_assign_client_on_failure(
        self, mock_cls: MagicMock,
    ) -> None:
        """_connect() should not leave _client set if __aenter__ raises."""
        mock_instance = AsyncMock()
        mock_instance.__aenter__ = AsyncMock(
            side_effect=ConnectionRefusedError("refused"),
        )
        mock_cls.return_value = mock_instance

        channel = MqttNotificationChannel(_make_config())
        with pytest.raises(ConnectionRefusedError):
            await channel._connect()

        assert channel._client is None


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


class TestDispatcher:
    async def test_dispatch_to_single_channel(self) -> None:
        stub = _StubChannel()
        dispatcher = NotificationDispatcher([stub])
        notification = _make_notification()

        results = await dispatcher.dispatch(notification)

        assert results == {"stub": True}
        assert len(stub.sent) == 1

    async def test_dispatch_to_multiple_channels(self) -> None:
        ch1 = _StubChannel("ch1")
        ch2 = _StubChannel("ch2")
        dispatcher = NotificationDispatcher([ch1, ch2])

        results = await dispatcher.dispatch(_make_notification())

        assert results == {"ch1": True, "ch2": True}

    async def test_one_channel_failure_doesnt_block_others(self) -> None:
        healthy = _StubChannel("healthy")
        failing = _FailingChannel(fail_on="send")
        dispatcher = NotificationDispatcher([failing, healthy])

        results = await dispatcher.dispatch(_make_notification())

        assert results["healthy"] is True
        assert results["failing"] is False
        assert len(healthy.sent) == 1

    async def test_channel_returning_false(self) -> None:
        ch = _StubChannel("ch", success=False)
        dispatcher = NotificationDispatcher([ch])

        results = await dispatcher.dispatch(_make_notification())

        assert results == {"ch": False}


# ---------------------------------------------------------------------------
# Dispatcher lifecycle
# ---------------------------------------------------------------------------


class TestDispatcherLifecycle:
    async def test_start_starts_all_channels(self) -> None:
        ch1 = _StubChannel("ch1")
        ch2 = _StubChannel("ch2")
        dispatcher = NotificationDispatcher([ch1, ch2])

        await dispatcher.start()
        # No error = success (stub start is a no-op)

    async def test_start_failure_doesnt_block_others(self) -> None:
        healthy = _StubChannel("healthy")
        failing = _FailingChannel(fail_on="start")
        dispatcher = NotificationDispatcher([failing, healthy])

        await dispatcher.start()
        # Should not raise — failing channel logged, healthy started

    async def test_stop_failure_doesnt_block_others(self) -> None:
        healthy = _StubChannel("healthy")
        failing = _FailingChannel(fail_on="stop")
        dispatcher = NotificationDispatcher([failing, healthy])

        await dispatcher.stop()
        # Should not raise

    async def test_empty_dispatcher(self) -> None:
        dispatcher = NotificationDispatcher([])

        await dispatcher.start()
        results = await dispatcher.dispatch(_make_notification())
        await dispatcher.stop()

        assert results == {}

    def test_channels_property(self) -> None:
        ch1 = _StubChannel("ch1")
        ch2 = _StubChannel("ch2")
        dispatcher = NotificationDispatcher([ch1, ch2])

        channels = dispatcher.channels
        assert len(channels) == 2
        # Returns a copy, not the internal list
        channels.pop()
        assert len(dispatcher.channels) == 2


# ---------------------------------------------------------------------------
# MQTT channel: publish_raw
# ---------------------------------------------------------------------------


class TestMqttPublishRaw:
    async def test_publish_raw_delegates_to_client(self) -> None:
        channel = _mock_mqtt_channel()

        result = await channel.publish_raw(
            "oasis/pending/abc", '{"id": "abc"}', qos=1, retain=True
        )

        assert result is True
        channel._client.publish.assert_awaited_once_with(
            topic="oasis/pending/abc",
            payload='{"id": "abc"}',
            qos=1,
            retain=True,
        )

    async def test_publish_raw_retain_default_false(self) -> None:
        channel = _mock_mqtt_channel()

        await channel.publish_raw("some/topic", b"payload")

        call_kwargs = channel._client.publish.call_args.kwargs
        assert call_kwargs["retain"] is False

    async def test_publish_raw_without_start_returns_false(self) -> None:
        channel = MqttNotificationChannel(_make_config())

        result = await channel.publish_raw("topic", "payload")

        assert result is False

    async def test_publish_raw_failure_returns_false(self) -> None:
        channel = _mock_mqtt_channel()
        channel._client.publish.side_effect = Exception("broker down")

        result = await channel.publish_raw("topic", "payload")

        assert result is False

    async def test_publish_raw_empty_payload(self) -> None:
        """Empty payload clears a retained message."""
        channel = _mock_mqtt_channel()

        result = await channel.publish_raw(
            "oasis/pending/abc", b"", qos=1, retain=True
        )

        assert result is True
        call_kwargs = channel._client.publish.call_args.kwargs
        assert call_kwargs["payload"] == b""
        assert call_kwargs["retain"] is True
