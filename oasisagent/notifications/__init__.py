"""Notification dispatch — alert channels for escalations and status updates."""

from oasisagent.notifications.base import NotificationChannel
from oasisagent.notifications.dispatcher import NotificationDispatcher
from oasisagent.notifications.email import EmailNotificationChannel
from oasisagent.notifications.mqtt import MqttNotificationChannel
from oasisagent.notifications.webhook import WebhookNotificationChannel

__all__ = [
    "EmailNotificationChannel",
    "MqttNotificationChannel",
    "NotificationChannel",
    "NotificationDispatcher",
    "WebhookNotificationChannel",
]
