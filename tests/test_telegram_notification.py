"""Tests for TelegramChannel — notification + interactive approval."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from oasisagent.approval.pending import (
    ApprovalDecision,
    PendingAction,
    PendingStatus,
)
from oasisagent.config import TelegramNotificationConfig
from oasisagent.models import Notification, RecommendedAction, Severity
from oasisagent.notifications.interactive import InteractiveNotificationChannel
from oasisagent.notifications.telegram import TelegramChannel

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(**overrides: Any) -> TelegramNotificationConfig:
    defaults: dict[str, Any] = {
        "enabled": True,
        "bot_token": "123456:ABC-DEF",
        "chat_id": "-1001234567890",
        "parse_mode": "HTML",
    }
    defaults.update(overrides)
    return TelegramNotificationConfig(**defaults)


def _make_notification(**overrides: Any) -> Notification:
    defaults: dict[str, Any] = {
        "title": "Test Alert",
        "message": "Something happened",
        "severity": Severity.WARNING,
        "event_id": "evt-001",
    }
    defaults.update(overrides)
    return Notification(**defaults)


def _make_pending(**overrides: Any) -> PendingAction:
    defaults: dict[str, Any] = {
        "event_id": "evt-001",
        "action": RecommendedAction(
            handler="homeassistant",
            operation="restart_integration",
            params={"integration": "zwave"},
            description="Restart Z-Wave integration",
            risk_tier="recommend",
        ),
        "diagnosis": "Z-Wave integration is unresponsive",
        "expires_at": datetime.now(UTC) + timedelta(minutes=30),
    }
    defaults.update(overrides)
    return PendingAction(**defaults)


def _make_channel_with_mock_bot(
    **config_overrides: Any,
) -> tuple[TelegramChannel, AsyncMock]:
    """Create a TelegramChannel with a mocked Bot."""
    config = _make_config(**config_overrides)
    channel = TelegramChannel(config)
    mock_bot = AsyncMock()
    mock_bot.session = AsyncMock()
    channel._bot = mock_bot
    return channel, mock_bot


# ---------------------------------------------------------------------------
# Config model tests
# ---------------------------------------------------------------------------


class TestTelegramNotificationConfig:
    def test_defaults(self) -> None:
        config = TelegramNotificationConfig()
        assert config.enabled is False
        assert config.bot_token == ""
        assert config.chat_id == ""
        assert config.parse_mode == "HTML"

    def test_custom_values(self) -> None:
        config = _make_config()
        assert config.enabled is True
        assert config.bot_token == "123456:ABC-DEF"
        assert config.chat_id == "-1001234567890"

    def test_extra_fields_forbidden(self) -> None:
        with pytest.raises(ValueError, match="Extra inputs"):
            TelegramNotificationConfig(unknown_field="value")  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Channel identity tests
# ---------------------------------------------------------------------------


class TestTelegramChannelIdentity:
    def test_name(self) -> None:
        channel = TelegramChannel(_make_config())
        assert channel.name() == "telegram"

    def test_is_interactive(self) -> None:
        channel = TelegramChannel(_make_config())
        assert isinstance(channel, InteractiveNotificationChannel)


# ---------------------------------------------------------------------------
# Health check tests
# ---------------------------------------------------------------------------


class TestTelegramHealthy:
    async def test_healthy_disabled(self) -> None:
        channel = TelegramChannel(_make_config(enabled=False))
        assert await channel.healthy() is True

    async def test_healthy_no_bot(self) -> None:
        channel = TelegramChannel(_make_config())
        # _bot is None (not started)
        assert await channel.healthy() is True

    async def test_healthy_success(self) -> None:
        channel, mock_bot = _make_channel_with_mock_bot()
        mock_me = MagicMock()
        mock_me.username = "oasis_bot"
        mock_bot.me = AsyncMock(return_value=mock_me)

        assert await channel.healthy() is True
        mock_bot.me.assert_awaited_once()

    async def test_healthy_failure(self) -> None:
        channel, mock_bot = _make_channel_with_mock_bot()
        mock_bot.me = AsyncMock(side_effect=Exception("Unauthorized"))

        assert await channel.healthy() is False


# ---------------------------------------------------------------------------
# Lifecycle tests
# ---------------------------------------------------------------------------


class TestTelegramChannelLifecycle:
    async def test_start_disabled(self) -> None:
        channel = TelegramChannel(_make_config(enabled=False))
        await channel.start()
        assert channel._bot is None

    async def test_start_no_token(self) -> None:
        channel = TelegramChannel(_make_config(bot_token=""))
        await channel.start()
        assert channel._bot is None

    @patch("oasisagent.notifications.telegram.Bot")
    async def test_start_verifies_token(self, mock_bot_cls: MagicMock) -> None:
        mock_bot = AsyncMock()
        mock_me = MagicMock()
        mock_me.username = "oasis_test_bot"
        mock_bot.me = AsyncMock(return_value=mock_me)
        mock_bot.session = AsyncMock()
        mock_bot_cls.return_value = mock_bot

        channel = TelegramChannel(_make_config())
        await channel.start()

        assert channel._bot is mock_bot
        mock_bot.me.assert_awaited_once()

    @patch("oasisagent.notifications.telegram.Bot")
    async def test_start_invalid_token_raises(self, mock_bot_cls: MagicMock) -> None:
        mock_bot = AsyncMock()
        mock_bot.me = AsyncMock(side_effect=Exception("Unauthorized"))
        mock_bot.session = AsyncMock()
        mock_bot_cls.return_value = mock_bot

        channel = TelegramChannel(_make_config())
        with pytest.raises(Exception, match="Unauthorized"):
            await channel.start()
        assert channel._bot is None

    async def test_stop_closes_session(self) -> None:
        channel, mock_bot = _make_channel_with_mock_bot()
        await channel.stop()
        mock_bot.session.close.assert_awaited_once()
        assert channel._bot is None

    async def test_stop_noop_when_not_started(self) -> None:
        channel = TelegramChannel(_make_config())
        await channel.stop()  # Should not raise


# ---------------------------------------------------------------------------
# Send notification tests
# ---------------------------------------------------------------------------


class TestTelegramSend:
    async def test_send_disabled(self) -> None:
        channel = TelegramChannel(_make_config(enabled=False))
        result = await channel.send(_make_notification())
        assert result is True  # disabled = no-op success

    async def test_send_no_bot(self) -> None:
        channel = TelegramChannel(_make_config())
        # _bot is None (not started)
        result = await channel.send(_make_notification())
        assert result is True  # disabled path

    async def test_send_no_chat_id(self) -> None:
        channel, _ = _make_channel_with_mock_bot(chat_id="")
        result = await channel.send(_make_notification())
        assert result is False

    async def test_send_success(self) -> None:
        channel, mock_bot = _make_channel_with_mock_bot()
        notification = _make_notification()

        result = await channel.send(notification)

        assert result is True
        mock_bot.send_message.assert_awaited_once()
        call_kwargs = mock_bot.send_message.call_args.kwargs
        assert call_kwargs["chat_id"] == "-1001234567890"
        assert "Test Alert" in call_kwargs["text"]

    async def test_send_failure(self) -> None:
        channel, mock_bot = _make_channel_with_mock_bot()
        mock_bot.send_message.side_effect = Exception("Network error")

        result = await channel.send(_make_notification())

        assert result is False

    async def test_send_formats_severity_emoji(self) -> None:
        channel, mock_bot = _make_channel_with_mock_bot()

        await channel.send(_make_notification(severity=Severity.CRITICAL))
        text = mock_bot.send_message.call_args.kwargs["text"]
        assert "\U0001f534" in text  # 🔴

    async def test_send_includes_event_id(self) -> None:
        channel, mock_bot = _make_channel_with_mock_bot()

        await channel.send(_make_notification(event_id="evt-42"))
        text = mock_bot.send_message.call_args.kwargs["text"]
        assert "evt-42" in text


# ---------------------------------------------------------------------------
# Approval request tests
# ---------------------------------------------------------------------------


class TestTelegramApprovalRequest:
    async def test_send_approval_disabled(self) -> None:
        channel = TelegramChannel(_make_config(enabled=False))
        pending = _make_pending()
        await channel.send_approval_request(pending)  # Should not raise

    async def test_send_approval_no_chat_id(self) -> None:
        channel, _ = _make_channel_with_mock_bot(chat_id="")
        await channel.send_approval_request(_make_pending())
        # Should not raise, just log warning

    async def test_send_approval_success(self) -> None:
        channel, mock_bot = _make_channel_with_mock_bot()
        mock_msg = MagicMock()
        mock_msg.message_id = 42
        mock_bot.send_message.return_value = mock_msg

        pending = _make_pending()
        await channel.send_approval_request(pending)

        mock_bot.send_message.assert_awaited_once()
        call_kwargs = mock_bot.send_message.call_args.kwargs
        assert call_kwargs["chat_id"] == "-1001234567890"
        assert "Requires Approval" in call_kwargs["text"]
        assert "Restart Z-Wave" in call_kwargs["text"]

        # Check inline keyboard
        keyboard = call_kwargs["reply_markup"]
        buttons = keyboard.inline_keyboard[0]
        assert len(buttons) == 2
        assert buttons[0].text == "\u2705 Approve"
        assert buttons[0].callback_data == f"approve:{pending.id}"
        assert buttons[1].text == "\u274c Reject"
        assert buttons[1].callback_data == f"reject:{pending.id}"

    async def test_send_approval_tracks_message_id(self) -> None:
        channel, mock_bot = _make_channel_with_mock_bot()
        mock_msg = MagicMock()
        mock_msg.message_id = 99
        mock_bot.send_message.return_value = mock_msg

        pending = _make_pending()
        await channel.send_approval_request(pending)

        assert channel._approval_messages[pending.id] == 99

    async def test_send_approval_failure(self) -> None:
        channel, mock_bot = _make_channel_with_mock_bot()
        mock_bot.send_message.side_effect = Exception("API error")

        pending = _make_pending()
        await channel.send_approval_request(pending)  # Should not raise
        assert pending.id not in channel._approval_messages

    async def test_callback_data_fits_64_bytes(self) -> None:
        """Telegram limits callback_data to 64 bytes."""
        pending = _make_pending()
        approve_data = f"approve:{pending.id}"
        reject_data = f"reject:{pending.id}"
        assert len(approve_data.encode()) <= 64
        assert len(reject_data.encode()) <= 64


# ---------------------------------------------------------------------------
# Update approval message tests
# ---------------------------------------------------------------------------


class TestTelegramUpdateApproval:
    async def test_update_edits_message(self) -> None:
        channel, mock_bot = _make_channel_with_mock_bot()
        channel._approval_messages["action-1"] = 42

        await channel.update_approval_message("action-1", PendingStatus.APPROVED)

        mock_bot.edit_message_text.assert_awaited_once()
        call_kwargs = mock_bot.edit_message_text.call_args.kwargs
        assert call_kwargs["message_id"] == 42
        assert "Approved" in call_kwargs["text"]
        assert "action-1" not in channel._approval_messages

    async def test_update_rejected(self) -> None:
        channel, mock_bot = _make_channel_with_mock_bot()
        channel._approval_messages["action-2"] = 55

        await channel.update_approval_message("action-2", PendingStatus.REJECTED)

        text = mock_bot.edit_message_text.call_args.kwargs["text"]
        assert "Rejected" in text

    async def test_update_expired(self) -> None:
        channel, mock_bot = _make_channel_with_mock_bot()
        channel._approval_messages["action-3"] = 77

        await channel.update_approval_message("action-3", PendingStatus.EXPIRED)

        text = mock_bot.edit_message_text.call_args.kwargs["text"]
        assert "Expired" in text

    async def test_update_unknown_action_noop(self) -> None:
        channel, mock_bot = _make_channel_with_mock_bot()
        await channel.update_approval_message("unknown", PendingStatus.APPROVED)
        mock_bot.edit_message_text.assert_not_awaited()

    async def test_update_failure_does_not_raise(self) -> None:
        channel, mock_bot = _make_channel_with_mock_bot()
        channel._approval_messages["action-4"] = 88
        mock_bot.edit_message_text.side_effect = Exception("Message deleted")

        await channel.update_approval_message("action-4", PendingStatus.APPROVED)
        # Should not raise


# ---------------------------------------------------------------------------
# Callback handler tests
# ---------------------------------------------------------------------------


class TestTelegramCallbackHandlers:
    async def test_approve_callback(self) -> None:
        channel, _ = _make_channel_with_mock_bot()
        callback = AsyncMock()
        channel._approval_callback = callback

        query = AsyncMock(spec=["data", "answer"])
        query.data = "approve:action-123"
        query.answer = AsyncMock()

        await channel._handle_approve(query)

        query.answer.assert_awaited_once_with("Approved")
        callback.assert_awaited_once_with("action-123", ApprovalDecision.APPROVED)

    async def test_reject_callback(self) -> None:
        channel, _ = _make_channel_with_mock_bot()
        callback = AsyncMock()
        channel._approval_callback = callback

        query = AsyncMock(spec=["data", "answer"])
        query.data = "reject:action-456"
        query.answer = AsyncMock()

        await channel._handle_reject(query)

        query.answer.assert_awaited_once_with("Rejected")
        callback.assert_awaited_once_with("action-456", ApprovalDecision.REJECTED)

    async def test_callback_with_no_data(self) -> None:
        channel, _ = _make_channel_with_mock_bot()
        callback = AsyncMock()
        channel._approval_callback = callback

        query = AsyncMock(spec=["data", "answer"])
        query.data = None

        await channel._handle_approve(query)
        callback.assert_not_awaited()

    async def test_callback_without_registered_handler(self) -> None:
        channel, _ = _make_channel_with_mock_bot()
        # No callback registered

        query = AsyncMock(spec=["data", "answer"])
        query.data = "approve:action-789"
        query.answer = AsyncMock()

        await channel._handle_approve(query)
        query.answer.assert_awaited_once()  # Still answers the query


# ---------------------------------------------------------------------------
# Status command tests
# ---------------------------------------------------------------------------


class TestTelegramStatusCommand:
    async def test_status_command(self) -> None:
        channel, _ = _make_channel_with_mock_bot()
        channel._approval_messages["a1"] = 1
        channel._approval_messages["a2"] = 2

        message = AsyncMock()
        message.reply = AsyncMock()

        await channel._handle_status(message)

        message.reply.assert_awaited_once()
        text = message.reply.call_args.kwargs["text"]
        assert "Pending approvals: 2" in text
        assert "connected" in text


# ---------------------------------------------------------------------------
# Formatting tests
# ---------------------------------------------------------------------------


class TestTelegramFormatting:
    def test_notification_format(self) -> None:
        channel = TelegramChannel(_make_config())
        notification = _make_notification(
            title="Disk Full",
            message="Root partition at 95%",
            severity=Severity.CRITICAL,
            event_id="evt-disk-1",
        )
        text = channel._format_notification(notification)
        assert "\U0001f534" in text  # 🔴
        assert "<b>Disk Full</b>" in text
        assert "Root partition at 95%" in text
        assert "evt-disk-1" in text

    def test_approval_format(self) -> None:
        channel = TelegramChannel(_make_config())
        pending = _make_pending()
        text = channel._format_approval(pending)
        assert "Requires Approval" in text
        assert "Restart Z-Wave" in text
        assert "homeassistant" in text
        assert "restart_integration" in text
        assert "recommend" in text
        assert pending.id in text


# ---------------------------------------------------------------------------
# Rate limiting tests
# ---------------------------------------------------------------------------


class TestTelegramRateLimit:
    async def test_rate_limit_updates_timestamp(self) -> None:
        channel, _ = _make_channel_with_mock_bot()
        assert channel._last_send_time == 0.0

        await channel._rate_limit()
        assert channel._last_send_time > 0.0


# ---------------------------------------------------------------------------
# Listener lifecycle tests
# ---------------------------------------------------------------------------


class TestTelegramListenerLifecycle:
    async def test_start_listener_disabled(self) -> None:
        channel = TelegramChannel(_make_config(enabled=False))
        await channel.start_listener(AsyncMock())
        assert channel._polling_task is None

    async def test_stop_listener_noop_when_not_started(self) -> None:
        channel = TelegramChannel(_make_config())
        await channel.stop_listener()  # Should not raise

    async def test_stop_listener_cancels_task(self) -> None:
        channel, _ = _make_channel_with_mock_bot()

        # Simulate a running polling task
        async def _fake_poll() -> None:
            await asyncio.sleep(100)

        channel._polling_task = asyncio.create_task(_fake_poll())

        await channel.stop_listener()
        assert channel._polling_task is None


# ---------------------------------------------------------------------------
# Registry tests
# ---------------------------------------------------------------------------


class TestTelegramRegistry:
    def test_registered_in_notification_types(self) -> None:
        from oasisagent.db.registry import NOTIFICATION_TYPES

        assert "telegram" in NOTIFICATION_TYPES
        meta = NOTIFICATION_TYPES["telegram"]
        assert meta.model is TelegramNotificationConfig
        assert "bot_token" in meta.secret_fields
