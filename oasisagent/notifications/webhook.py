"""Webhook notification channel — POSTs JSON to configured URLs.

Retries on 5xx responses with exponential backoff (3 attempts).
Each URL is independent — failure on one does not affect the others.

ARCHITECTURE.md §10 defines the notification contract.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

import aiohttp

from oasisagent.notifications.base import NotificationChannel

if TYPE_CHECKING:
    from oasisagent.config import WebhookNotificationConfig
    from oasisagent.models import Notification

logger = logging.getLogger(__name__)

# Retry configuration
_MAX_ATTEMPTS = 3
_BACKOFF_BASE = 1.0  # seconds
_BACKOFF_MULTIPLIER = 2.0
_REQUEST_TIMEOUT = 10  # seconds


class WebhookNotificationChannel(NotificationChannel):
    """Sends notifications as JSON POST requests to webhook URLs.

    Must be started with ``await channel.start()`` before use.
    Retries on 5xx with exponential backoff. 4xx errors are not retried.
    """

    def __init__(self, config: WebhookNotificationConfig) -> None:
        self._config = config
        self._session: aiohttp.ClientSession | None = None

    def name(self) -> str:
        return "webhook"

    async def start(self) -> None:
        """Create the aiohttp session."""
        timeout = aiohttp.ClientTimeout(total=_REQUEST_TIMEOUT)
        self._session = aiohttp.ClientSession(timeout=timeout)
        logger.info(
            "Webhook notification channel started (urls=%d)",
            len(self._config.urls),
        )

    async def stop(self) -> None:
        """Close the aiohttp session."""
        if self._session is not None:
            await self._session.close()
            self._session = None
            logger.info("Webhook notification channel stopped")

    async def send(self, notification: Notification) -> bool:
        """POST the notification as JSON to all configured URLs.

        Returns True if ALL URLs succeeded, False if any failed.
        Best-effort: failures are logged, not raised.
        """
        if not self._config.urls:
            return True

        if self._session is None:
            logger.warning(
                "Webhook channel not started — dropping notification %s", notification.id
            )
            return False

        payload = self._build_payload(notification)
        all_ok = True

        for url in self._config.urls:
            ok = await self._post_with_retry(url, payload, notification.id)
            if not ok:
                all_ok = False

        return all_ok

    async def _post_with_retry(
        self, url: str, payload: dict[str, Any], notification_id: str
    ) -> bool:
        """POST to a single URL with retry on 5xx."""
        assert self._session is not None
        delay = _BACKOFF_BASE

        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                async with self._session.post(url, json=payload) as resp:
                    if resp.status < 500:
                        if resp.status < 400:
                            logger.debug(
                                "Webhook delivered: url=%s, id=%s, status=%d",
                                url, notification_id, resp.status,
                            )
                            return True
                        # 4xx — don't retry
                        logger.warning(
                            "Webhook rejected (4xx): url=%s, id=%s, status=%d",
                            url, notification_id, resp.status,
                        )
                        return False

                    # 5xx — retry
                    logger.warning(
                        "Webhook 5xx (attempt %d/%d): url=%s, status=%d",
                        attempt, _MAX_ATTEMPTS, url, resp.status,
                    )
            except (aiohttp.ClientError, TimeoutError) as exc:
                logger.warning(
                    "Webhook error (attempt %d/%d): url=%s, %s",
                    attempt, _MAX_ATTEMPTS, url, exc,
                )

            if attempt < _MAX_ATTEMPTS:
                await asyncio.sleep(delay)
                delay *= _BACKOFF_MULTIPLIER

        logger.warning(
            "Webhook exhausted retries: url=%s, id=%s",
            url, notification_id,
        )
        return False

    def _build_payload(self, notification: Notification) -> dict[str, Any]:
        """Build the JSON payload from a Notification."""
        return {
            "id": notification.id,
            "event_id": notification.event_id,
            "severity": notification.severity.value,
            "title": notification.title,
            "message": notification.message,
            "timestamp": notification.timestamp.isoformat(),
            "metadata": notification.metadata,
        }
