"""Discord webhook notification channel — sends embeds to Discord.

Posts JSON embeds to a configured Discord webhook URL using aiohttp.
No bot token or gateway connection required — webhook-only.

Severity-mapped embed colors, event summary fields, and timestamp footer.
Retries on 5xx with exponential backoff (3 attempts).

ARCHITECTURE.md §10 defines the notification contract.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC
from typing import TYPE_CHECKING, Any

import aiohttp

from oasisagent.notifications.base import NotificationChannel

if TYPE_CHECKING:
    from oasisagent.config import DiscordNotificationConfig
    from oasisagent.models import Notification

logger = logging.getLogger(__name__)

# Retry configuration
_MAX_ATTEMPTS = 3
_BACKOFF_BASE = 1.0  # seconds
_BACKOFF_MULTIPLIER = 2.0
_REQUEST_TIMEOUT = 10  # seconds

# Discord limits
_CONTENT_MAX_LENGTH = 2000
_EMBED_TOTAL_MAX_LENGTH = 6000
_EMBED_DESCRIPTION_MAX_LENGTH = 4096
_EMBED_FIELD_VALUE_MAX_LENGTH = 1024

# Severity → Discord embed color
_SEVERITY_COLORS: dict[str, int] = {
    "critical": 0xFF0000,  # red
    "error": 0xFF0000,     # red (same as critical)
    "warning": 0xFFA500,   # orange
    "info": 0x00FF00,      # green
    "debug": 0x808080,     # gray
}


def _truncate(text: str, max_length: int) -> str:
    """Truncate text to max_length, appending ellipsis if trimmed."""
    if len(text) <= max_length:
        return text
    return text[: max_length - 3] + "..."


class DiscordNotificationChannel(NotificationChannel):
    """Sends notifications as Discord webhook embeds.

    Must be started with ``await channel.start()`` before use.
    Retries on 5xx/429 with exponential backoff. 4xx errors are not retried.
    """

    def __init__(self, config: DiscordNotificationConfig) -> None:
        self._config = config
        self._session: aiohttp.ClientSession | None = None
        self._last_send_ok: bool | None = None

    def name(self) -> str:
        return "discord"

    async def start(self) -> None:
        """Create the aiohttp session."""
        if not self._config.enabled:
            logger.info("Discord notifications disabled — skipping")
            return

        timeout = aiohttp.ClientTimeout(total=_REQUEST_TIMEOUT)
        self._session = aiohttp.ClientSession(timeout=timeout)
        logger.info("Discord notification channel started")

    async def stop(self) -> None:
        """Close the aiohttp session."""
        if self._session is not None:
            await self._session.close()
            self._session = None
            logger.info("Discord notification channel stopped")

    async def healthy(self) -> bool:
        """Check Discord webhook health.

        Uses GET on the webhook URL to verify it exists. Discord returns
        webhook metadata on GET. Falls back to last-send tracking if
        no session is available.
        """
        if not self._config.enabled:
            return True

        if self._session is None:
            return False

        # If we've never sent, try a GET to verify the webhook exists
        if self._last_send_ok is None:
            try:
                async with self._session.get(self._config.webhook_url) as resp:
                    self._last_send_ok = resp.status < 400
            except (aiohttp.ClientError, TimeoutError):
                self._last_send_ok = False

        return self._last_send_ok is True

    async def send(self, notification: Notification) -> bool:
        """Send a notification as a Discord embed.

        Returns True on success, False on failure (best-effort).
        """
        if not self._config.enabled:
            return True

        if self._session is None:
            logger.warning(
                "Discord channel not started — dropping notification %s",
                notification.id,
            )
            return False

        payload = self._build_payload(notification)
        ok = await self._post_with_retry(payload, notification.id)
        self._last_send_ok = ok
        return ok

    async def _post_with_retry(
        self, payload: dict[str, Any], notification_id: str,
    ) -> bool:
        """POST to the Discord webhook with retry on 5xx/429."""
        assert self._session is not None
        delay = _BACKOFF_BASE

        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                async with self._session.post(
                    self._config.webhook_url, json=payload,
                ) as resp:
                    if resp.status == 429:
                        # Rate limited — retry with Discord's suggested wait
                        retry_after = 1.0
                        try:
                            body = await resp.json()
                            retry_after = float(body.get("retry_after", 1.0))
                        except Exception:
                            pass
                        logger.warning(
                            "Discord rate limited (attempt %d/%d): retry_after=%.1fs",
                            attempt, _MAX_ATTEMPTS, retry_after,
                        )
                        if attempt < _MAX_ATTEMPTS:
                            await asyncio.sleep(retry_after)
                        continue

                    if resp.status < 400:
                        logger.debug(
                            "Discord notification sent: id=%s, status=%d",
                            notification_id, resp.status,
                        )
                        return True

                    if resp.status < 500:
                        # 4xx — don't retry
                        body_text = await resp.text()
                        logger.warning(
                            "Discord webhook rejected (4xx): id=%s, status=%d, body=%s",
                            notification_id, resp.status, body_text[:200],
                        )
                        return False

                    # 5xx — retry
                    logger.warning(
                        "Discord webhook 5xx (attempt %d/%d): status=%d",
                        attempt, _MAX_ATTEMPTS, resp.status,
                    )
            except (aiohttp.ClientError, TimeoutError) as exc:
                logger.warning(
                    "Discord webhook error (attempt %d/%d): %s",
                    attempt, _MAX_ATTEMPTS, exc,
                )

            if attempt < _MAX_ATTEMPTS:
                await asyncio.sleep(delay)
                delay *= _BACKOFF_MULTIPLIER

        logger.warning(
            "Discord webhook exhausted retries: id=%s", notification_id,
        )
        return False

    def _build_payload(self, notification: Notification) -> dict[str, Any]:
        """Build the Discord webhook JSON payload with an embed."""
        severity = notification.severity.value
        color = _SEVERITY_COLORS.get(severity, 0x808080)

        description = _truncate(
            notification.message, _EMBED_DESCRIPTION_MAX_LENGTH,
        )

        embed: dict[str, Any] = {
            "title": _truncate(notification.title, 256),
            "description": description,
            "color": color,
            "timestamp": notification.timestamp.astimezone(UTC).isoformat(),
            "footer": {"text": f"OasisAgent | {severity.upper()}"},
        }

        # Add fields for metadata
        fields: list[dict[str, Any]] = []
        if notification.event_id:
            fields.append({
                "name": "Event ID",
                "value": f"`{notification.event_id}`",
                "inline": True,
            })
        fields.append({
            "name": "Severity",
            "value": severity.upper(),
            "inline": True,
        })

        # Include any extra metadata as fields
        for key, value in notification.metadata.items():
            field_value = _truncate(str(value), _EMBED_FIELD_VALUE_MAX_LENGTH)
            fields.append({
                "name": key,
                "value": field_value,
                "inline": True,
            })

        if fields:
            embed["fields"] = fields

        payload: dict[str, Any] = {
            "embeds": [embed],
        }

        if self._config.username:
            payload["username"] = self._config.username
        if self._config.avatar_url:
            payload["avatar_url"] = self._config.avatar_url

        return payload
