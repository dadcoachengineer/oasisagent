"""Slack notification channel — posts messages via Incoming Webhooks.

Sends notifications as Slack Block Kit formatted messages with severity
color bars, event details, and timestamps. Uses aiohttp to POST to
the configured webhook URL.

Slack Incoming Webhooks don't support a ping/health endpoint, so
healthy() tracks the last send result (similar to the LLM health
pattern).

ARCHITECTURE.md §10 defines the notification contract.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

import aiohttp

from oasisagent.notifications.base import NotificationChannel

if TYPE_CHECKING:
    from oasisagent.config import SlackNotificationConfig
    from oasisagent.models import Notification

logger = logging.getLogger(__name__)

# Retry configuration (matches webhook channel)
_MAX_ATTEMPTS = 3
_BACKOFF_BASE = 1.0  # seconds
_BACKOFF_MULTIPLIER = 2.0
_REQUEST_TIMEOUT = 10  # seconds

# Severity → Slack color (hex sidebar attachment color)
_SEVERITY_COLORS: dict[str, str] = {
    "critical": "#E01E5A",  # red
    "error": "#E01E5A",     # red
    "warning": "#ECB22E",   # yellow
    "info": "#36C5F0",      # blue
}


class SlackNotificationChannel(NotificationChannel):
    """Sends notifications to Slack via Incoming Webhooks.

    Must be started with ``await channel.start()`` before use.
    Retries on 5xx with exponential backoff. 4xx errors are not retried.
    """

    def __init__(self, config: SlackNotificationConfig) -> None:
        self._config = config
        self._session: aiohttp.ClientSession | None = None
        self._last_send_ok: bool | None = None

    def name(self) -> str:
        return "slack"

    async def healthy(self) -> bool:
        """Return True if webhook_url is configured and last send succeeded.

        Slack Incoming Webhooks have no health/ping endpoint, so we track
        the result of the most recent send. Before the first send, returns
        True if the webhook URL is configured (optimistic).
        """
        if not self._config.enabled:
            return True
        if not self._config.webhook_url:
            return False
        if self._last_send_ok is None:
            return True  # optimistic before first send
        return self._last_send_ok

    async def start(self) -> None:
        """Create the aiohttp session."""
        if not self._config.enabled:
            logger.info("Slack notifications disabled — skipping setup")
            return

        timeout = aiohttp.ClientTimeout(total=_REQUEST_TIMEOUT)
        self._session = aiohttp.ClientSession(timeout=timeout)
        logger.info("Slack notification channel started")

    async def stop(self) -> None:
        """Close the aiohttp session."""
        if self._session is not None:
            await self._session.close()
            self._session = None
            logger.info("Slack notification channel stopped")

    async def send(self, notification: Notification) -> bool:
        """POST a Block Kit formatted message to the Slack webhook.

        Returns True on success, False on failure (best-effort).
        """
        if not self._config.enabled:
            return True

        if not self._config.webhook_url:
            logger.warning(
                "Slack webhook_url not configured — dropping notification %s",
                notification.id,
            )
            self._last_send_ok = False
            return False

        if self._session is None:
            logger.warning(
                "Slack channel not started — dropping notification %s",
                notification.id,
            )
            self._last_send_ok = False
            return False

        payload = self._build_payload(notification)
        ok = await self._post_with_retry(payload, notification.id)
        self._last_send_ok = ok
        return ok

    async def _post_with_retry(
        self, payload: dict[str, Any], notification_id: str,
    ) -> bool:
        """POST to the Slack webhook with retry on 5xx."""
        assert self._session is not None
        delay = _BACKOFF_BASE

        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                async with self._session.post(
                    self._config.webhook_url, json=payload,
                ) as resp:
                    if resp.status < 400:
                        logger.debug(
                            "Slack notification delivered: id=%s, status=%d",
                            notification_id, resp.status,
                        )
                        return True

                    if resp.status < 500:
                        # 4xx — don't retry (bad webhook URL, invalid payload)
                        body = await resp.text()
                        logger.warning(
                            "Slack webhook rejected (4xx): id=%s, status=%d,"
                            " body=%s",
                            notification_id, resp.status, body[:200],
                        )
                        return False

                    # 5xx — retry
                    logger.warning(
                        "Slack webhook 5xx (attempt %d/%d): status=%d",
                        attempt, _MAX_ATTEMPTS, resp.status,
                    )
            except (aiohttp.ClientError, TimeoutError) as exc:
                logger.warning(
                    "Slack webhook error (attempt %d/%d): %s",
                    attempt, _MAX_ATTEMPTS, exc,
                )

            if attempt < _MAX_ATTEMPTS:
                await asyncio.sleep(delay)
                delay *= _BACKOFF_MULTIPLIER

        logger.warning(
            "Slack webhook exhausted retries: id=%s", notification_id,
        )
        return False

    def _build_payload(self, notification: Notification) -> dict[str, Any]:
        """Build a Slack Block Kit payload from a Notification."""
        severity = notification.severity.value
        color = _SEVERITY_COLORS.get(severity, "#808080")
        timestamp = notification.timestamp.strftime("%Y-%m-%d %H:%M:%S UTC")

        # Build the blocks for the attachment
        blocks: list[dict[str, Any]] = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": notification.title,
                    "emoji": True,
                },
            },
            {
                "type": "section",
                "fields": [
                    {
                        "type": "mrkdwn",
                        "text": f"*Severity:*\n{severity.upper()}",
                    },
                    {
                        "type": "mrkdwn",
                        "text": (
                            f"*Event ID:*\n"
                            f"{notification.event_id or 'N/A'}"
                        ),
                    },
                ],
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": notification.message,
                },
            },
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f":clock1: {timestamp}",
                    },
                ],
            },
        ]

        payload: dict[str, Any] = {
            "attachments": [
                {
                    "color": color,
                    "blocks": blocks,
                },
            ],
        }

        # Optional overrides
        if self._config.channel:
            payload["channel"] = self._config.channel
        if self._config.username:
            payload["username"] = self._config.username
        if self._config.icon_emoji:
            payload["icon_emoji"] = self._config.icon_emoji

        return payload
