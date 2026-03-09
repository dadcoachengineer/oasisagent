"""Tests for the approval listener MQTT subscriber."""

from __future__ import annotations

from unittest.mock import MagicMock

import aiomqtt

from oasisagent.approval.listener import ApprovalListener
from oasisagent.config import MqttIngestionConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config() -> MqttIngestionConfig:
    return MqttIngestionConfig(
        enabled=True,
        broker="mqtt://localhost:1883",
        username="",
        password="",
        client_id="test-agent",
    )


def _make_listener(
    on_approve: MagicMock | None = None,
    on_reject: MagicMock | None = None,
) -> ApprovalListener:
    return ApprovalListener(
        config=_make_config(),
        on_approve=on_approve or MagicMock(),
        on_reject=on_reject or MagicMock(),
    )


def _make_mqtt_message(topic: str, payload: bytes = b"") -> aiomqtt.Message:
    """Create a mock MQTT message with the given topic."""
    return aiomqtt.Message(
        topic=topic,
        payload=payload,
        qos=1,
        retain=False,
        mid=0,
        properties=None,
    )


# ---------------------------------------------------------------------------
# Topic parsing
# ---------------------------------------------------------------------------


class TestHandleMessage:
    def test_approve_message_calls_on_approve(self) -> None:
        on_approve = MagicMock()
        listener = _make_listener(on_approve=on_approve)

        msg = _make_mqtt_message("oasis/approve/abc-123")
        listener._handle_message(msg)

        on_approve.assert_called_once_with("abc-123")

    def test_reject_message_calls_on_reject(self) -> None:
        on_reject = MagicMock()
        listener = _make_listener(on_reject=on_reject)

        msg = _make_mqtt_message("oasis/reject/abc-123")
        listener._handle_message(msg)

        on_reject.assert_called_once_with("abc-123")

    def test_unknown_action_type_ignored(self) -> None:
        on_approve = MagicMock()
        on_reject = MagicMock()
        listener = _make_listener(on_approve=on_approve, on_reject=on_reject)

        msg = _make_mqtt_message("oasis/cancel/abc-123")
        listener._handle_message(msg)

        on_approve.assert_not_called()
        on_reject.assert_not_called()

    def test_wrong_prefix_ignored(self) -> None:
        on_approve = MagicMock()
        listener = _make_listener(on_approve=on_approve)

        msg = _make_mqtt_message("other/approve/abc-123")
        listener._handle_message(msg)

        on_approve.assert_not_called()

    def test_too_few_topic_parts_ignored(self) -> None:
        on_approve = MagicMock()
        listener = _make_listener(on_approve=on_approve)

        msg = _make_mqtt_message("oasis/approve")
        listener._handle_message(msg)

        on_approve.assert_not_called()

    def test_too_many_topic_parts_ignored(self) -> None:
        on_approve = MagicMock()
        listener = _make_listener(on_approve=on_approve)

        msg = _make_mqtt_message("oasis/approve/abc/extra")
        listener._handle_message(msg)

        on_approve.assert_not_called()

    def test_uuid_action_id_preserved(self) -> None:
        on_approve = MagicMock()
        listener = _make_listener(on_approve=on_approve)

        action_id = "550e8400-e29b-41d4-a716-446655440000"
        msg = _make_mqtt_message(f"oasis/approve/{action_id}")
        listener._handle_message(msg)

        on_approve.assert_called_once_with(action_id)


# ---------------------------------------------------------------------------
# Connection config
# ---------------------------------------------------------------------------


class TestListenerConfig:
    def test_parses_broker_url(self) -> None:
        listener = _make_listener()
        assert listener._hostname == "localhost"
        assert listener._port == 1883

    def test_parses_custom_port(self) -> None:
        config = MqttIngestionConfig(
            enabled=True,
            broker="mqtt://broker.local:1884",
            client_id="test",
        )
        listener = ApprovalListener(
            config=config,
            on_approve=MagicMock(),
            on_reject=MagicMock(),
        )
        assert listener._hostname == "broker.local"
        assert listener._port == 1884

    def test_initial_state(self) -> None:
        listener = _make_listener()
        assert listener.connected is False

    async def test_stop_sets_stopping(self) -> None:
        listener = _make_listener()
        await listener.stop()
        assert listener._stopping is True
