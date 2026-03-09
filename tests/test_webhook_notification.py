"""Tests for the webhook notification channel."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp

from oasisagent.config import WebhookNotificationConfig
from oasisagent.models import Notification, Severity
from oasisagent.notifications.webhook import WebhookNotificationChannel


def _make_config(**overrides: object) -> WebhookNotificationConfig:
    defaults = {
        "enabled": True,
        "urls": ["https://hooks.example.com/oasis"],
    }
    defaults.update(overrides)
    return WebhookNotificationConfig.model_validate(defaults)


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


def _mock_response(status: int = 200) -> MagicMock:
    """Create a mock aiohttp response with the given status."""
    resp = MagicMock()
    resp.status = status
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


class TestWebhookChannelName:
    async def test_name_returns_webhook(self) -> None:
        channel = WebhookNotificationChannel(_make_config())
        assert channel.name() == "webhook"


class TestWebhookStartStop:
    async def test_start_creates_session(self) -> None:
        channel = WebhookNotificationChannel(_make_config())
        await channel.start()
        assert channel._session is not None
        await channel.stop()

    async def test_stop_closes_session(self) -> None:
        channel = WebhookNotificationChannel(_make_config())
        await channel.start()
        session = channel._session
        await channel.stop()
        assert channel._session is None
        assert session is not None
        assert session.closed


class TestWebhookSend:
    async def test_send_posts_json_to_each_url(self) -> None:
        config = _make_config(urls=["https://a.example.com", "https://b.example.com"])
        channel = WebhookNotificationChannel(config)
        await channel.start()

        notification = _make_notification()
        resp = _mock_response(200)

        with patch.object(channel._session, "post", return_value=resp) as mock_post:
            result = await channel.send(notification)

        assert result is True
        assert mock_post.call_count == 2
        urls_called = [call.args[0] for call in mock_post.call_args_list]
        assert "https://a.example.com" in urls_called
        assert "https://b.example.com" in urls_called
        await channel.stop()

    async def test_payload_contains_expected_fields(self) -> None:
        channel = WebhookNotificationChannel(_make_config())
        await channel.start()

        notification = _make_notification()
        resp = _mock_response(200)

        with patch.object(channel._session, "post", return_value=resp) as mock_post:
            await channel.send(notification)

        _, kwargs = mock_post.call_args
        payload = kwargs["json"]
        assert payload["id"] == "notif-1"
        assert payload["event_id"] == "evt-1"
        assert payload["severity"] == "error"
        assert payload["title"] == "container crash"
        assert payload["message"] == "Container nginx exited with code 137"
        assert "timestamp" in payload
        await channel.stop()

    @patch("oasisagent.notifications.webhook.asyncio.sleep", new_callable=AsyncMock)
    async def test_retries_on_5xx(self, mock_sleep: AsyncMock) -> None:
        channel = WebhookNotificationChannel(_make_config())
        await channel.start()

        notification = _make_notification()
        resp_500 = _mock_response(500)
        resp_200 = _mock_response(200)

        call_count = 0

        def side_effect(*args: object, **kwargs: object) -> MagicMock:
            nonlocal call_count
            call_count += 1
            return resp_500 if call_count <= 2 else resp_200

        with patch.object(channel._session, "post", side_effect=side_effect):
            result = await channel.send(notification)

        assert result is True
        assert call_count == 3
        assert mock_sleep.await_count == 2
        await channel.stop()

    async def test_no_retry_on_4xx(self) -> None:
        channel = WebhookNotificationChannel(_make_config())
        await channel.start()

        notification = _make_notification()
        resp = _mock_response(400)

        with patch.object(channel._session, "post", return_value=resp) as mock_post:
            result = await channel.send(notification)

        assert result is False
        assert mock_post.call_count == 1
        await channel.stop()

    @patch("oasisagent.notifications.webhook.asyncio.sleep", new_callable=AsyncMock)
    async def test_succeeds_after_5xx_retry(self, mock_sleep: AsyncMock) -> None:
        channel = WebhookNotificationChannel(_make_config())
        await channel.start()

        notification = _make_notification()
        resp_503 = _mock_response(503)
        resp_200 = _mock_response(200)

        responses = [resp_503, resp_200]
        idx = 0

        def side_effect(*args: object, **kwargs: object) -> MagicMock:
            nonlocal idx
            r = responses[idx]
            idx += 1
            return r

        with patch.object(channel._session, "post", side_effect=side_effect):
            result = await channel.send(notification)

        assert result is True
        await channel.stop()

    @patch("oasisagent.notifications.webhook.asyncio.sleep", new_callable=AsyncMock)
    async def test_handles_connection_error(self, mock_sleep: AsyncMock) -> None:
        channel = WebhookNotificationChannel(_make_config())
        await channel.start()

        notification = _make_notification()

        with patch.object(
            channel._session, "post", side_effect=aiohttp.ClientError("Connection refused")
        ):
            result = await channel.send(notification)

        assert result is False
        await channel.stop()

    @patch("oasisagent.notifications.webhook.asyncio.sleep", new_callable=AsyncMock)
    async def test_handles_timeout(self, mock_sleep: AsyncMock) -> None:
        channel = WebhookNotificationChannel(_make_config())
        await channel.start()

        notification = _make_notification()

        with patch.object(channel._session, "post", side_effect=TimeoutError("timed out")):
            result = await channel.send(notification)

        assert result is False
        await channel.stop()

    async def test_continues_to_next_url_after_failure(self) -> None:
        config = _make_config(urls=["https://fail.example.com", "https://ok.example.com"])
        channel = WebhookNotificationChannel(config)
        await channel.start()

        notification = _make_notification()
        resp_ok = _mock_response(200)

        call_count = 0

        def side_effect(*args: object, **kwargs: object) -> MagicMock:
            nonlocal call_count
            call_count += 1
            url = args[0] if args else kwargs.get("url", "")
            if url == "https://fail.example.com":
                return _mock_response(400)
            return resp_ok

        with (
            patch.object(channel._session, "post", side_effect=side_effect),
        ):
            result = await channel.send(notification)

        # One URL failed, so overall result is False
        assert result is False
        # But both URLs were attempted
        assert call_count == 2
        await channel.stop()

    async def test_empty_urls_is_noop(self) -> None:
        config = _make_config(urls=[])
        channel = WebhookNotificationChannel(config)
        await channel.start()

        notification = _make_notification()
        result = await channel.send(notification)

        assert result is True
        await channel.stop()

    async def test_send_without_start_returns_false(self) -> None:
        channel = WebhookNotificationChannel(_make_config())
        notification = _make_notification()

        result = await channel.send(notification)

        assert result is False


class TestWebhookOrchestratorRegistration:
    async def test_enabled_channel_registered(self) -> None:
        """Webhook channel is added to dispatcher when enabled."""
        from oasisagent.config import OasisAgentConfig
        from oasisagent.orchestrator import Orchestrator

        config = OasisAgentConfig.model_validate({
            "notifications": {
                "mqtt": {"enabled": False},
                "webhook": {
                    "enabled": True,
                    "urls": ["https://hooks.example.com/oasis"],
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

        orch = Orchestrator(config)
        orch._build_components()

        assert orch._dispatcher is not None
        names = [c.name() for c in orch._dispatcher.channels]
        assert "webhook" in names

    async def test_disabled_channel_not_registered(self) -> None:
        """Webhook channel is not added when disabled."""
        from oasisagent.config import OasisAgentConfig
        from oasisagent.orchestrator import Orchestrator

        config = OasisAgentConfig.model_validate({
            "notifications": {
                "mqtt": {"enabled": False},
                "webhook": {"enabled": False},
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

        orch = Orchestrator(config)
        orch._build_components()

        assert orch._dispatcher is not None
        names = [c.name() for c in orch._dispatcher.channels]
        assert "webhook" not in names
