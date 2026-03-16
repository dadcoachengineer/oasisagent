"""Tests for the Slack notification channel."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

from oasisagent.config import SlackNotificationConfig
from oasisagent.models import Notification, Severity
from oasisagent.notifications.slack import SlackNotificationChannel

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(**overrides: Any) -> SlackNotificationConfig:
    defaults: dict[str, Any] = {
        "enabled": True,
        "webhook_url": "https://hooks.slack.com/services/T00/B00/xxx",
        "channel": "#alerts",
        "username": "OasisAgent",
        "icon_emoji": ":robot_face:",
    }
    defaults.update(overrides)
    return SlackNotificationConfig(**defaults)


def _make_notification(**overrides: Any) -> Notification:
    defaults: dict[str, Any] = {
        "title": "Test Alert",
        "message": "Something happened",
        "severity": Severity.WARNING,
        "event_id": "evt-001",
    }
    defaults.update(overrides)
    return Notification(**defaults)


def _mock_slack_channel(
    config: SlackNotificationConfig | None = None,
    status: int = 200,
    body: str = "ok",
) -> SlackNotificationChannel:
    """Create a Slack channel with a mocked aiohttp session."""
    channel = SlackNotificationChannel(config or _make_config())

    mock_response = AsyncMock()
    mock_response.status = status
    mock_response.text = AsyncMock(return_value=body)
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=False)

    mock_session = AsyncMock()
    mock_session.post = MagicMock(return_value=mock_response)

    channel._session = mock_session
    return channel


# ---------------------------------------------------------------------------
# name
# ---------------------------------------------------------------------------


class TestSlackName:
    def test_name_is_slack(self) -> None:
        channel = SlackNotificationChannel(_make_config())
        assert channel.name() == "slack"


# ---------------------------------------------------------------------------
# healthy
# ---------------------------------------------------------------------------


class TestSlackHealthy:
    async def test_healthy_when_disabled(self) -> None:
        channel = SlackNotificationChannel(_make_config(enabled=False))
        assert await channel.healthy() is True

    async def test_unhealthy_when_no_webhook_url(self) -> None:
        channel = SlackNotificationChannel(_make_config(webhook_url=""))
        assert await channel.healthy() is False

    async def test_healthy_before_first_send(self) -> None:
        channel = SlackNotificationChannel(_make_config())
        assert await channel.healthy() is True

    async def test_healthy_after_successful_send(self) -> None:
        channel = _mock_slack_channel()
        await channel.send(_make_notification())
        assert await channel.healthy() is True

    async def test_unhealthy_after_failed_send(self) -> None:
        channel = _mock_slack_channel(status=500)
        await channel.send(_make_notification())
        assert await channel.healthy() is False


# ---------------------------------------------------------------------------
# lifecycle
# ---------------------------------------------------------------------------


class TestSlackLifecycle:
    async def test_start_creates_session(self) -> None:
        channel = SlackNotificationChannel(_make_config())
        await channel.start()
        assert channel._session is not None
        await channel.stop()

    async def test_start_disabled_skips_session(self) -> None:
        channel = SlackNotificationChannel(_make_config(enabled=False))
        await channel.start()
        assert channel._session is None

    async def test_stop_closes_session(self) -> None:
        channel = SlackNotificationChannel(_make_config())
        await channel.start()
        assert channel._session is not None
        await channel.stop()
        assert channel._session is None

    async def test_stop_without_start_is_noop(self) -> None:
        channel = SlackNotificationChannel(_make_config())
        await channel.stop()  # Should not raise


# ---------------------------------------------------------------------------
# send
# ---------------------------------------------------------------------------


class TestSlackSend:
    async def test_send_returns_true_on_success(self) -> None:
        channel = _mock_slack_channel()
        result = await channel.send(_make_notification())
        assert result is True

    async def test_send_disabled_returns_true(self) -> None:
        channel = SlackNotificationChannel(_make_config(enabled=False))
        result = await channel.send(_make_notification())
        assert result is True

    async def test_send_no_webhook_url_returns_false(self) -> None:
        channel = _mock_slack_channel(config=_make_config(webhook_url=""))
        result = await channel.send(_make_notification())
        assert result is False

    async def test_send_without_start_returns_false(self) -> None:
        channel = SlackNotificationChannel(_make_config())
        result = await channel.send(_make_notification())
        assert result is False

    async def test_send_posts_to_webhook_url(self) -> None:
        channel = _mock_slack_channel()
        await channel.send(_make_notification())
        channel._session.post.assert_called_once()
        call_args = channel._session.post.call_args
        assert call_args[0][0] == "https://hooks.slack.com/services/T00/B00/xxx"

    async def test_send_4xx_returns_false(self) -> None:
        channel = _mock_slack_channel(status=403)
        result = await channel.send(_make_notification())
        assert result is False

    async def test_send_5xx_retries_and_fails(self) -> None:
        """5xx responses trigger retries. After exhausting, returns False."""
        channel = _mock_slack_channel(status=500)
        result = await channel.send(_make_notification())
        assert result is False
        # Should have been called 3 times (initial + 2 retries)
        assert channel._session.post.call_count == 3

    async def test_send_network_error_retries(self) -> None:
        """Network errors trigger retries."""
        import aiohttp

        channel = _mock_slack_channel()
        mock_response = AsyncMock()
        mock_response.__aenter__ = AsyncMock(
            side_effect=aiohttp.ClientError("connection reset"),
        )
        mock_response.__aexit__ = AsyncMock(return_value=False)
        channel._session.post = MagicMock(return_value=mock_response)

        result = await channel.send(_make_notification())
        assert result is False
        assert channel._session.post.call_count == 3


# ---------------------------------------------------------------------------
# message formatting
# ---------------------------------------------------------------------------


class TestSlackPayload:
    async def test_payload_has_attachments_with_blocks(self) -> None:
        channel = _mock_slack_channel()
        notification = _make_notification(
            title="Test Alert", severity=Severity.ERROR,
        )
        payload = channel._build_payload(notification)

        assert "attachments" in payload
        attachment = payload["attachments"][0]
        assert attachment["color"] == "#E01E5A"  # red for error
        assert len(attachment["blocks"]) == 4

    async def test_payload_header_contains_title(self) -> None:
        channel = _mock_slack_channel()
        notification = _make_notification(title="DB Connection Lost")
        payload = channel._build_payload(notification)

        header = payload["attachments"][0]["blocks"][0]
        assert header["type"] == "header"
        assert header["text"]["text"] == "DB Connection Lost"

    async def test_payload_severity_in_fields(self) -> None:
        channel = _mock_slack_channel()
        notification = _make_notification(severity=Severity.CRITICAL)
        payload = channel._build_payload(notification)

        fields_block = payload["attachments"][0]["blocks"][1]
        severity_field = fields_block["fields"][0]
        assert "CRITICAL" in severity_field["text"]

    async def test_payload_event_id_in_fields(self) -> None:
        channel = _mock_slack_channel()
        notification = _make_notification(event_id="evt-123")
        payload = channel._build_payload(notification)

        fields_block = payload["attachments"][0]["blocks"][1]
        event_field = fields_block["fields"][1]
        assert "evt-123" in event_field["text"]

    async def test_payload_message_in_section(self) -> None:
        channel = _mock_slack_channel()
        notification = _make_notification(message="Server unreachable")
        payload = channel._build_payload(notification)

        message_block = payload["attachments"][0]["blocks"][2]
        assert message_block["text"]["text"] == "Server unreachable"

    async def test_payload_includes_channel_override(self) -> None:
        channel = _mock_slack_channel()
        payload = channel._build_payload(_make_notification())
        assert payload["channel"] == "#alerts"

    async def test_payload_includes_username(self) -> None:
        channel = _mock_slack_channel()
        payload = channel._build_payload(_make_notification())
        assert payload["username"] == "OasisAgent"

    async def test_payload_includes_icon_emoji(self) -> None:
        channel = _mock_slack_channel()
        payload = channel._build_payload(_make_notification())
        assert payload["icon_emoji"] == ":robot_face:"

    async def test_payload_no_channel_when_empty(self) -> None:
        channel = _mock_slack_channel(config=_make_config(channel=""))
        payload = channel._build_payload(_make_notification())
        assert "channel" not in payload

    async def test_payload_no_event_id_shows_na(self) -> None:
        channel = _mock_slack_channel()
        notification = _make_notification(event_id=None)
        payload = channel._build_payload(notification)

        fields_block = payload["attachments"][0]["blocks"][1]
        event_field = fields_block["fields"][1]
        assert "N/A" in event_field["text"]

    async def test_severity_colors(self) -> None:
        channel = _mock_slack_channel()

        for severity, expected_color in [
            (Severity.CRITICAL, "#E01E5A"),
            (Severity.ERROR, "#E01E5A"),
            (Severity.WARNING, "#ECB22E"),
            (Severity.INFO, "#36C5F0"),
        ]:
            notification = _make_notification(severity=severity)
            payload = channel._build_payload(notification)
            assert payload["attachments"][0]["color"] == expected_color, (
                f"Wrong color for {severity}"
            )

    async def test_context_block_has_timestamp(self) -> None:
        channel = _mock_slack_channel()
        notification = _make_notification()
        payload = channel._build_payload(notification)

        context = payload["attachments"][0]["blocks"][3]
        assert context["type"] == "context"
        assert ":clock1:" in context["elements"][0]["text"]
