"""Tests for Discord webhook notification channel."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from oasisagent.config import DiscordNotificationConfig
from oasisagent.models import Notification, Severity
from oasisagent.notifications.discord import (
    _SEVERITY_COLORS,
    DiscordNotificationChannel,
    _truncate,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(**overrides: Any) -> DiscordNotificationConfig:
    defaults: dict[str, Any] = {
        "enabled": True,
        "webhook_url": "https://discord.com/api/webhooks/123/abc",
        "username": "OasisAgent",
        "avatar_url": "",
    }
    defaults.update(overrides)
    return DiscordNotificationConfig(**defaults)


def _make_notification(**overrides: Any) -> Notification:
    defaults: dict[str, Any] = {
        "title": "Test Alert",
        "message": "Something happened in the home lab",
        "severity": Severity.WARNING,
        "event_id": "evt-001",
    }
    defaults.update(overrides)
    return Notification(**defaults)


def _mock_discord_channel(
    *, send_status: int = 204,
) -> DiscordNotificationChannel:
    """Create a Discord channel with a mocked aiohttp session."""
    channel = DiscordNotificationChannel(_make_config())

    mock_response = AsyncMock()
    mock_response.status = send_status
    mock_response.text = AsyncMock(return_value="")
    mock_response.json = AsyncMock(return_value={})
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=False)

    mock_session = AsyncMock()
    mock_session.post = MagicMock(return_value=mock_response)
    mock_session.get = MagicMock(return_value=mock_response)

    channel._session = mock_session
    return channel


# ---------------------------------------------------------------------------
# Channel name
# ---------------------------------------------------------------------------


class TestDiscordName:
    def test_name_is_discord(self) -> None:
        channel = DiscordNotificationChannel(_make_config())
        assert channel.name() == "discord"


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestDiscordLifecycle:
    @patch("oasisagent.notifications.discord.aiohttp.ClientSession")
    async def test_start_creates_session(self, mock_cls: MagicMock) -> None:
        channel = DiscordNotificationChannel(_make_config())
        await channel.start()

        mock_cls.assert_called_once()
        assert channel._session is not None

    async def test_start_disabled_skips(self) -> None:
        channel = DiscordNotificationChannel(_make_config(enabled=False))
        await channel.start()

        assert channel._session is None

    async def test_stop_closes_session(self) -> None:
        channel = _mock_discord_channel()
        session = channel._session

        await channel.stop()

        assert channel._session is None
        session.close.assert_awaited_once()

    async def test_stop_without_start_is_noop(self) -> None:
        channel = DiscordNotificationChannel(_make_config())
        await channel.stop()  # Should not raise


# ---------------------------------------------------------------------------
# Send
# ---------------------------------------------------------------------------


class TestDiscordSend:
    async def test_send_posts_to_webhook(self) -> None:
        channel = _mock_discord_channel()
        notification = _make_notification()

        result = await channel.send(notification)

        assert result is True
        channel._session.post.assert_called_once()
        call_args = channel._session.post.call_args
        assert call_args[0][0] == "https://discord.com/api/webhooks/123/abc"

    async def test_send_disabled_returns_true(self) -> None:
        channel = DiscordNotificationChannel(_make_config(enabled=False))

        result = await channel.send(_make_notification())

        assert result is True

    async def test_send_without_start_returns_false(self) -> None:
        channel = DiscordNotificationChannel(_make_config())

        result = await channel.send(_make_notification())

        assert result is False

    async def test_send_updates_last_send_ok(self) -> None:
        channel = _mock_discord_channel()
        assert channel._last_send_ok is None

        await channel.send(_make_notification())

        assert channel._last_send_ok is True

    async def test_send_failure_updates_last_send_ok(self) -> None:
        channel = _mock_discord_channel(send_status=400)

        await channel.send(_make_notification())

        assert channel._last_send_ok is False


# ---------------------------------------------------------------------------
# Embed formatting
# ---------------------------------------------------------------------------


class TestDiscordEmbed:
    async def test_embed_has_title(self) -> None:
        channel = _mock_discord_channel()
        notification = _make_notification(title="Server Down")

        await channel.send(notification)

        payload = channel._session.post.call_args[1]["json"]
        embed = payload["embeds"][0]
        assert embed["title"] == "Server Down"

    async def test_embed_has_description(self) -> None:
        channel = _mock_discord_channel()
        notification = _make_notification(message="HA integration failed")

        await channel.send(notification)

        payload = channel._session.post.call_args[1]["json"]
        embed = payload["embeds"][0]
        assert embed["description"] == "HA integration failed"

    async def test_embed_has_timestamp(self) -> None:
        channel = _mock_discord_channel()

        await channel.send(_make_notification())

        payload = channel._session.post.call_args[1]["json"]
        embed = payload["embeds"][0]
        assert "timestamp" in embed

    async def test_embed_has_footer(self) -> None:
        channel = _mock_discord_channel()

        await channel.send(_make_notification(severity=Severity.CRITICAL))

        payload = channel._session.post.call_args[1]["json"]
        embed = payload["embeds"][0]
        assert embed["footer"]["text"] == "OasisAgent | CRITICAL"

    async def test_embed_has_event_id_field(self) -> None:
        channel = _mock_discord_channel()

        await channel.send(_make_notification(event_id="evt-xyz"))

        payload = channel._session.post.call_args[1]["json"]
        embed = payload["embeds"][0]
        event_field = next(f for f in embed["fields"] if f["name"] == "Event ID")
        assert event_field["value"] == "`evt-xyz`"

    async def test_embed_has_severity_field(self) -> None:
        channel = _mock_discord_channel()

        await channel.send(_make_notification(severity=Severity.CRITICAL))

        payload = channel._session.post.call_args[1]["json"]
        embed = payload["embeds"][0]
        sev_field = next(f for f in embed["fields"] if f["name"] == "Severity")
        assert sev_field["value"] == "CRITICAL"

    async def test_embed_includes_metadata(self) -> None:
        channel = _mock_discord_channel()
        notification = _make_notification(metadata={"entity": "sensor.temp"})

        await channel.send(notification)

        payload = channel._session.post.call_args[1]["json"]
        embed = payload["embeds"][0]
        entity_field = next(f for f in embed["fields"] if f["name"] == "entity")
        assert entity_field["value"] == "sensor.temp"

    async def test_username_included_in_payload(self) -> None:
        channel = _mock_discord_channel()

        await channel.send(_make_notification())

        payload = channel._session.post.call_args[1]["json"]
        assert payload["username"] == "OasisAgent"

    async def test_avatar_url_included_when_set(self) -> None:
        channel = DiscordNotificationChannel(
            _make_config(avatar_url="https://example.com/avatar.png")
        )
        channel._session = _mock_discord_channel()._session

        await channel.send(_make_notification())

        payload = channel._session.post.call_args[1]["json"]
        assert payload["avatar_url"] == "https://example.com/avatar.png"

    async def test_avatar_url_omitted_when_empty(self) -> None:
        channel = _mock_discord_channel()

        await channel.send(_make_notification())

        payload = channel._session.post.call_args[1]["json"]
        assert "avatar_url" not in payload


# ---------------------------------------------------------------------------
# Severity colors
# ---------------------------------------------------------------------------


class TestDiscordSeverityColors:
    @pytest.mark.parametrize(
        ("severity", "expected_color"),
        [
            (Severity.CRITICAL, 0xFF0000),
            (Severity.ERROR, 0xFF0000),
            (Severity.WARNING, 0xFFA500),
            (Severity.INFO, 0x00FF00),
        ],
    )
    async def test_severity_color(
        self, severity: Severity, expected_color: int,
    ) -> None:
        channel = _mock_discord_channel()

        await channel.send(_make_notification(severity=severity))

        payload = channel._session.post.call_args[1]["json"]
        embed = payload["embeds"][0]
        assert embed["color"] == expected_color

    def test_color_map_has_all_severities(self) -> None:
        for sev in Severity:
            assert sev.value in _SEVERITY_COLORS


# ---------------------------------------------------------------------------
# Truncation
# ---------------------------------------------------------------------------


class TestTruncation:
    def test_short_text_unchanged(self) -> None:
        assert _truncate("hello", 100) == "hello"

    def test_exact_length_unchanged(self) -> None:
        text = "a" * 100
        assert _truncate(text, 100) == text

    def test_long_text_truncated(self) -> None:
        text = "a" * 200
        result = _truncate(text, 100)
        assert len(result) == 100
        assert result.endswith("...")

    async def test_long_description_truncated_in_embed(self) -> None:
        channel = _mock_discord_channel()
        long_msg = "x" * 5000
        notification = _make_notification(message=long_msg)

        await channel.send(notification)

        payload = channel._session.post.call_args[1]["json"]
        embed = payload["embeds"][0]
        assert len(embed["description"]) <= 4096

    async def test_long_title_truncated_in_embed(self) -> None:
        channel = _mock_discord_channel()
        long_title = "T" * 300
        notification = _make_notification(title=long_title)

        await channel.send(notification)

        payload = channel._session.post.call_args[1]["json"]
        embed = payload["embeds"][0]
        assert len(embed["title"]) <= 256


# ---------------------------------------------------------------------------
# Error handling and retries
# ---------------------------------------------------------------------------


class TestDiscordErrors:
    async def test_4xx_not_retried(self) -> None:
        channel = _mock_discord_channel(send_status=400)

        result = await channel.send(_make_notification())

        assert result is False
        # Only called once — no retry on 4xx
        assert channel._session.post.call_count == 1

    async def test_5xx_retried(self) -> None:
        channel = _mock_discord_channel(send_status=500)

        result = await channel.send(_make_notification())

        assert result is False
        assert channel._session.post.call_count == 3  # MAX_ATTEMPTS

    async def test_connection_error_retried(self) -> None:
        channel = _mock_discord_channel()
        import aiohttp

        mock_response = AsyncMock()
        mock_response.__aenter__ = AsyncMock(
            side_effect=aiohttp.ClientError("connection refused")
        )
        mock_response.__aexit__ = AsyncMock(return_value=False)
        channel._session.post = MagicMock(return_value=mock_response)

        result = await channel.send(_make_notification())

        assert result is False
        assert channel._session.post.call_count == 3

    async def test_429_rate_limit_retried(self) -> None:
        """429 responses should be retried with the suggested wait time."""
        channel = _mock_discord_channel(send_status=429)
        # Make the response return a retry_after value
        resp = channel._session.post.return_value
        resp.__aenter__.return_value.json = AsyncMock(
            return_value={"retry_after": 0.01}
        )
        resp.__aenter__.return_value.status = 429

        result = await channel.send(_make_notification())

        assert result is False
        assert channel._session.post.call_count == 3


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


class TestDiscordHealth:
    async def test_healthy_when_disabled(self) -> None:
        channel = DiscordNotificationChannel(_make_config(enabled=False))
        assert await channel.healthy() is True

    async def test_unhealthy_without_session(self) -> None:
        channel = DiscordNotificationChannel(_make_config())
        assert await channel.healthy() is False

    async def test_healthy_after_successful_send(self) -> None:
        channel = _mock_discord_channel()
        await channel.send(_make_notification())

        assert await channel.healthy() is True

    async def test_unhealthy_after_failed_send(self) -> None:
        channel = _mock_discord_channel(send_status=400)
        await channel.send(_make_notification())

        assert await channel.healthy() is False

    async def test_health_probes_webhook_on_first_check(self) -> None:
        """Before any send, healthy() GETs the webhook URL to verify it exists."""
        channel = _mock_discord_channel()

        result = await channel.healthy()

        assert result is True
        channel._session.get.assert_called_once_with(
            "https://discord.com/api/webhooks/123/abc"
        )

    async def test_health_probe_failure(self) -> None:
        """If the GET probe fails, healthy() returns False."""
        channel = _mock_discord_channel()
        import aiohttp

        mock_response = AsyncMock()
        mock_response.__aenter__ = AsyncMock(
            side_effect=aiohttp.ClientError("connection refused")
        )
        mock_response.__aexit__ = AsyncMock(return_value=False)
        channel._session.get = MagicMock(return_value=mock_response)

        result = await channel.healthy()

        assert result is False
