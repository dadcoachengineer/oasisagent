"""Ingestion adapters — transform external events into canonical Event objects."""

from oasisagent.ingestion.base import IngestAdapter
from oasisagent.ingestion.ha_log_poller import HaLogPollerAdapter
from oasisagent.ingestion.ha_websocket import HaWebSocketAdapter
from oasisagent.ingestion.mqtt import MqttAdapter

__all__ = [
    "HaLogPollerAdapter",
    "HaWebSocketAdapter",
    "IngestAdapter",
    "MqttAdapter",
]
