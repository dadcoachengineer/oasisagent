"""Email notification channel — sends notifications via SMTP.

Connect-per-send pattern: each send() opens a connection, sends the
message, and disconnects. This avoids stale connection issues with
SMTP servers and is appropriate for OasisAgent's low notification rate.

ARCHITECTURE.md §10 defines the notification contract.
"""

from __future__ import annotations

import logging
from email.message import EmailMessage
from typing import TYPE_CHECKING

import aiosmtplib

from oasisagent.notifications.base import NotificationChannel

if TYPE_CHECKING:
    from oasisagent.config import EmailNotificationConfig
    from oasisagent.models import Notification

logger = logging.getLogger(__name__)


class EmailNotificationChannel(NotificationChannel):
    """Sends notifications as plain-text emails via SMTP.

    Requires at least one recipient in ``config.to``. If the list is
    empty, ``send()`` returns True immediately (no-op).

    SMTP authentication is used when ``config.username`` is non-empty.
    STARTTLS is attempted by default (``config.starttls``).
    """

    def __init__(self, config: EmailNotificationConfig) -> None:
        self._config = config

    def name(self) -> str:
        return "email"

    async def send(self, notification: Notification) -> bool:
        """Compose and send an email notification.

        Returns True on success, False on failure (best-effort).
        """
        if not self._config.to:
            logger.debug("Email channel has no recipients — skipping")
            return True

        msg = self._build_message(notification)

        try:
            smtp = aiosmtplib.SMTP(
                hostname=self._config.smtp_host,
                port=self._config.smtp_port,
                start_tls=self._config.starttls,
            )
            await smtp.connect()

            if self._config.username:
                await smtp.login(self._config.username, self._config.password)

            await smtp.send_message(msg)
            await smtp.quit()

            logger.debug(
                "Email notification sent: id=%s, to=%s",
                notification.id,
                self._config.to,
            )
            return True
        except (aiosmtplib.SMTPException, OSError) as exc:
            logger.warning(
                "Email notification failed for %s: %s",
                notification.id,
                exc,
            )
            return False

    def _build_message(self, notification: Notification) -> EmailMessage:
        """Build an EmailMessage from a Notification."""
        severity_tag = notification.severity.value.upper()
        subject = f"[{severity_tag}] OasisAgent: {notification.title}"

        body_lines = [
            f"Severity: {notification.severity.value}",
            f"Timestamp: {notification.timestamp.isoformat()}",
        ]
        if notification.event_id:
            body_lines.append(f"Event ID: {notification.event_id}")
        body_lines.append("")
        body_lines.append(notification.message)

        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self._config.from_address
        msg["To"] = ", ".join(self._config.to)
        msg.set_content("\n".join(body_lines))
        return msg
