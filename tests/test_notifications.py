"""Tests for notification channels and dispatcher."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

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
    start_error: Exception | None = None,
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

    async def test_stop_disconnects(self) -> None:
        channel = _mock_mqtt_channel()
        channel._client.__aexit__ = AsyncMock()

        await channel.stop()

        assert channel._client is None

    async def test_stop_without_start_is_noop(self) -> None:
        channel = MqttNotificationChannel(_make_config())
        await channel.stop()  # Should not raise


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
