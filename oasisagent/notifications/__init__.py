"""Notification dispatch — alert channels for escalations and status updates."""

from oasisagent.notifications.base import NotificationChannel
from oasisagent.notifications.dispatcher import NotificationDispatcher
from oasisagent.notifications.mqtt import MqttNotificationChannel

__all__ = [
    "MqttNotificationChannel",
    "NotificationChannel",
    "NotificationDispatcher",
]
