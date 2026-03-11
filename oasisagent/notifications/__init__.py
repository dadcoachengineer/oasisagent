"""Notification dispatch — alert channels for escalations and status updates."""

from oasisagent.notifications.base import NotificationChannel
from oasisagent.notifications.dispatcher import NotificationDispatcher
from oasisagent.notifications.email import EmailNotificationChannel
from oasisagent.notifications.interactive import InteractiveNotificationChannel
from oasisagent.notifications.mqtt import MqttNotificationChannel
from oasisagent.notifications.telegram import TelegramChannel
from oasisagent.notifications.webhook import WebhookNotificationChannel

__all__ = [
    "EmailNotificationChannel",
    "InteractiveNotificationChannel",
    "MqttNotificationChannel",
    "NotificationChannel",
    "NotificationDispatcher",
    "TelegramChannel",
    "WebhookNotificationChannel",
]
