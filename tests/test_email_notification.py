"""Tests for the email notification channel."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import aiosmtplib

from oasisagent.config import EmailNotificationConfig
from oasisagent.models import Notification, Severity
from oasisagent.notifications.email import EmailNotificationChannel


def _make_config(**overrides: object) -> EmailNotificationConfig:
    defaults = {
        "enabled": True,
        "smtp_host": "mail.example.com",
        "smtp_port": 587,
        "username": "",
        "password": "",
        "starttls": True,
        "from": "oasis@example.com",
        "to": ["admin@example.com"],
    }
    defaults.update(overrides)
    return EmailNotificationConfig.model_validate(defaults)


def _make_notification(**overrides: object) -> Notification:
    defaults = {
        "id": "notif-1",
        "event_id": "evt-1",
        "severity": Severity.ERROR,
        "title": "container crash",
        "message": "Container nginx exited with code 137",
        "timestamp": datetime(2025, 1, 15, 12, 0, 0, tzinfo=UTC),
    }
    defaults.update(overrides)
    return Notification.model_validate(defaults)


class TestEmailChannelName:
    async def test_name_returns_email(self) -> None:
        channel = EmailNotificationChannel(_make_config())
        assert channel.name() == "email"


class TestEmailStartStop:
    async def test_start_is_noop(self) -> None:
        channel = EmailNotificationChannel(_make_config())
        await channel.start()  # Should not raise

    async def test_stop_is_noop(self) -> None:
        channel = EmailNotificationChannel(_make_config())
        await channel.stop()  # Should not raise


class TestEmailSend:
    async def test_send_constructs_correct_subject(self) -> None:
        channel = EmailNotificationChannel(_make_config())
        notification = _make_notification()

        with patch("oasisagent.notifications.email.aiosmtplib.SMTP") as mock_smtp_cls:
            mock_smtp = AsyncMock()
            mock_smtp_cls.return_value = mock_smtp

            await channel.send(notification)

            # Check the message passed to send_message
            call_args = mock_smtp.send_message.call_args
            msg = call_args[0][0]
            assert msg["Subject"] == "[ERROR] OasisAgent: container crash"

    async def test_send_body_contains_event_fields(self) -> None:
        channel = EmailNotificationChannel(_make_config())
        notification = _make_notification()

        with patch("oasisagent.notifications.email.aiosmtplib.SMTP") as mock_smtp_cls:
            mock_smtp = AsyncMock()
            mock_smtp_cls.return_value = mock_smtp

            await channel.send(notification)

            msg = mock_smtp.send_message.call_args[0][0]
            body = msg.get_content()
            assert "Event ID: evt-1" in body
            assert "Severity: error" in body
            assert "Container nginx exited with code 137" in body

    async def test_send_calls_smtp_sequence(self) -> None:
        channel = EmailNotificationChannel(_make_config())
        notification = _make_notification()

        with patch("oasisagent.notifications.email.aiosmtplib.SMTP") as mock_smtp_cls:
            mock_smtp = AsyncMock()
            mock_smtp_cls.return_value = mock_smtp

            result = await channel.send(notification)

            assert result is True
            mock_smtp.connect.assert_awaited_once()
            mock_smtp.send_message.assert_awaited_once()
            mock_smtp.quit.assert_awaited_once()
            # No login when username is empty
            mock_smtp.login.assert_not_awaited()

    async def test_send_to_all_recipients(self) -> None:
        config = _make_config(to=["a@example.com", "b@example.com"])
        channel = EmailNotificationChannel(config)
        notification = _make_notification()

        with patch("oasisagent.notifications.email.aiosmtplib.SMTP") as mock_smtp_cls:
            mock_smtp = AsyncMock()
            mock_smtp_cls.return_value = mock_smtp

            await channel.send(notification)

            msg = mock_smtp.send_message.call_args[0][0]
            assert msg["To"] == "a@example.com, b@example.com"

    async def test_send_handles_smtp_error(self) -> None:
        channel = EmailNotificationChannel(_make_config())
        notification = _make_notification()

        with patch("oasisagent.notifications.email.aiosmtplib.SMTP") as mock_smtp_cls:
            mock_smtp = AsyncMock()
            mock_smtp.connect.side_effect = aiosmtplib.SMTPException("Connection refused")
            mock_smtp_cls.return_value = mock_smtp

            result = await channel.send(notification)

            assert result is False

    async def test_send_skipped_when_no_recipients(self) -> None:
        config = _make_config(to=[])
        channel = EmailNotificationChannel(config)
        notification = _make_notification()

        with patch("oasisagent.notifications.email.aiosmtplib.SMTP") as mock_smtp_cls:
            result = await channel.send(notification)

            assert result is True
            mock_smtp_cls.assert_not_called()

    async def test_send_includes_action_metadata(self) -> None:
        channel = EmailNotificationChannel(_make_config())
        notification = _make_notification(
            metadata={"handler": "docker", "operation": "restart_container"},
        )

        with patch("oasisagent.notifications.email.aiosmtplib.SMTP") as mock_smtp_cls:
            mock_smtp = AsyncMock()
            mock_smtp_cls.return_value = mock_smtp

            await channel.send(notification)
            # Message builds without error
            mock_smtp.send_message.assert_awaited_once()

    async def test_send_omits_event_id_when_none(self) -> None:
        channel = EmailNotificationChannel(_make_config())
        notification = _make_notification(event_id=None)

        with patch("oasisagent.notifications.email.aiosmtplib.SMTP") as mock_smtp_cls:
            mock_smtp = AsyncMock()
            mock_smtp_cls.return_value = mock_smtp

            await channel.send(notification)

            msg = mock_smtp.send_message.call_args[0][0]
            body = msg.get_content()
            assert "Event ID" not in body

    async def test_send_from_address(self) -> None:
        config = _make_config(**{"from": "alerts@mylab.local"})
        channel = EmailNotificationChannel(config)
        notification = _make_notification()

        with patch("oasisagent.notifications.email.aiosmtplib.SMTP") as mock_smtp_cls:
            mock_smtp = AsyncMock()
            mock_smtp_cls.return_value = mock_smtp

            await channel.send(notification)

            msg = mock_smtp.send_message.call_args[0][0]
            assert msg["From"] == "alerts@mylab.local"


class TestEmailAuth:
    async def test_login_called_when_username_set(self) -> None:
        config = _make_config(username="user", password="pass")
        channel = EmailNotificationChannel(config)
        notification = _make_notification()

        with patch("oasisagent.notifications.email.aiosmtplib.SMTP") as mock_smtp_cls:
            mock_smtp = AsyncMock()
            mock_smtp_cls.return_value = mock_smtp

            await channel.send(notification)

            mock_smtp.login.assert_awaited_once_with("user", "pass")

    async def test_login_not_called_when_username_empty(self) -> None:
        config = _make_config(username="", password="")
        channel = EmailNotificationChannel(config)
        notification = _make_notification()

        with patch("oasisagent.notifications.email.aiosmtplib.SMTP") as mock_smtp_cls:
            mock_smtp = AsyncMock()
            mock_smtp_cls.return_value = mock_smtp

            await channel.send(notification)

            mock_smtp.login.assert_not_awaited()

    async def test_login_failure_returns_false(self) -> None:
        config = _make_config(username="user", password="wrong")
        channel = EmailNotificationChannel(config)
        notification = _make_notification()

        with patch("oasisagent.notifications.email.aiosmtplib.SMTP") as mock_smtp_cls:
            mock_smtp = AsyncMock()
            mock_smtp.login.side_effect = aiosmtplib.SMTPException("Auth failed")
            mock_smtp_cls.return_value = mock_smtp

            result = await channel.send(notification)

            assert result is False

    async def test_starttls_passed_to_smtp(self) -> None:
        config = _make_config(starttls=False)
        channel = EmailNotificationChannel(config)
        notification = _make_notification()

        with patch("oasisagent.notifications.email.aiosmtplib.SMTP") as mock_smtp_cls:
            mock_smtp = AsyncMock()
            mock_smtp_cls.return_value = mock_smtp

            await channel.send(notification)

            mock_smtp_cls.assert_called_once_with(
                hostname="mail.example.com",
                port=587,
                start_tls=False,
            )


class TestEmailOrchestratorRegistration:
    async def test_enabled_channel_registered(self) -> None:
        """Email channel is added to dispatcher when enabled."""
        from oasisagent.config import OasisAgentConfig

        config = OasisAgentConfig.model_validate({
            "notifications": {
                "mqtt": {"enabled": False},
                "email": {
                    "enabled": True,
                    "from": "test@example.com",
                    "to": ["admin@example.com"],
                },
            },
            "llm": {
                "triage": {
                    "base_url": "http://localhost:11434/v1",
                    "model": "test",
                    "api_key": "test",
                },
                "reasoning": {
                    "base_url": "http://localhost:11434/v1",
                    "model": "test",
                    "api_key": "test",
                },
            },
            "agent": {"correlation_window": 0},
        })

        from oasisagent.orchestrator import Orchestrator

        orch = Orchestrator(config)
        orch._build_components()

        assert orch._dispatcher is not None
        names = [c.name() for c in orch._dispatcher.channels]
        assert "email" in names

    async def test_disabled_channel_not_registered(self) -> None:
        """Email channel is not added when disabled."""
        from oasisagent.config import OasisAgentConfig

        config = OasisAgentConfig.model_validate({
            "notifications": {
                "mqtt": {"enabled": False},
                "email": {"enabled": False},
            },
            "llm": {
                "triage": {
                    "base_url": "http://localhost:11434/v1",
                    "model": "test",
                    "api_key": "test",
                },
                "reasoning": {
                    "base_url": "http://localhost:11434/v1",
                    "model": "test",
                    "api_key": "test",
                },
            },
            "agent": {"correlation_window": 0},
        })

        from oasisagent.orchestrator import Orchestrator

        orch = Orchestrator(config)
        orch._build_components()

        assert orch._dispatcher is not None
        names = [c.name() for c in orch._dispatcher.channels]
        assert "email" not in names
