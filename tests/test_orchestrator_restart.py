"""Tests for orchestrator restart_connector / restart_service methods."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from oasisagent.config import (
    AgentConfig,
    AuditConfig,
    GuardrailsConfig,
    HaHandlerConfig,
    HandlersConfig,
    HaWebSocketConfig,
    InfluxDbConfig,
    IngestionConfig,
    LlmConfig,
    LlmEndpointConfig,
    MqttIngestionConfig,
    MqttNotificationConfig,
    NotificationsConfig,
    OasisAgentConfig,
)
from oasisagent.orchestrator import Orchestrator

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(**overrides: Any) -> OasisAgentConfig:
    """Create a minimal config for testing."""
    return OasisAgentConfig(
        agent=AgentConfig(
            event_queue_size=100,
            shutdown_timeout=2,
            event_ttl=300,
            known_fixes_dir="/nonexistent",
        ),
        ingestion=IngestionConfig(
            mqtt=MqttIngestionConfig(enabled=False),
            ha_websocket=HaWebSocketConfig(enabled=False),
        ),
        llm=LlmConfig(
            triage=LlmEndpointConfig(
                base_url="http://localhost:11434/v1", model="test", api_key="test",
            ),
            reasoning=LlmEndpointConfig(
                base_url="http://localhost:11434/v1", model="test", api_key="test",
            ),
        ),
        handlers=HandlersConfig(homeassistant=HaHandlerConfig(enabled=False)),
        guardrails=GuardrailsConfig(),
        audit=AuditConfig(influxdb=InfluxDbConfig(enabled=False)),
        notifications=NotificationsConfig(mqtt=MqttNotificationConfig(enabled=False)),
    )


def _mock_adapter(name: str = "mqtt") -> MagicMock:
    """Create a mock adapter with start/stop/healthy."""
    adapter = MagicMock()
    adapter.name = name
    adapter.start = AsyncMock()
    adapter.stop = AsyncMock()
    adapter.healthy = AsyncMock(return_value=True)
    return adapter


def _mock_handler(name: str = "homeassistant") -> MagicMock:
    """Create a mock handler with start/stop/healthy/name."""
    handler = MagicMock()
    handler.name = MagicMock(return_value=name)
    handler.start = AsyncMock()
    handler.stop = AsyncMock()
    handler.healthy = AsyncMock(return_value=True)
    return handler


def _mock_store(
    *,
    connector_row: dict[str, Any] | None = None,
    service_row: dict[str, Any] | None = None,
    notification_row: dict[str, Any] | None = None,
    config: OasisAgentConfig | None = None,
) -> MagicMock:
    """Create a mock config store."""
    store = MagicMock()
    store.get_connector = AsyncMock(return_value=connector_row)
    store.get_service = AsyncMock(return_value=service_row)
    store.get_notification = AsyncMock(return_value=notification_row)
    store.load_config = AsyncMock(return_value=config or _make_config())
    return store


# ---------------------------------------------------------------------------
# Tests: restart_connector
# ---------------------------------------------------------------------------


class TestRestartConnector:
    async def test_no_config_store_returns_false(self) -> None:
        orch = Orchestrator(_make_config())
        orch._build_components()
        result = await orch.restart_connector(1)
        assert result is False

    async def test_connector_not_found_returns_false(self) -> None:
        store = _mock_store(connector_row=None)
        orch = Orchestrator(_make_config(), config_store=store)
        orch._build_components()
        result = await orch.restart_connector(999)
        assert result is False
        store.get_connector.assert_awaited_once_with(999)

    async def test_stops_old_adapter_and_starts_new(self) -> None:
        config = _make_config()
        store = _mock_store(
            connector_row={
                "id": 1, "type": "mqtt", "name": "mqtt-primary",
                "enabled": True, "config": {},
            },
            config=config,
        )

        orch = Orchestrator(config, config_store=store)
        orch._build_components()

        # Inject a mock adapter
        old_adapter = _mock_adapter("mqtt")
        orch._adapters = [old_adapter]
        orch._adapter_tasks = [asyncio.create_task(asyncio.sleep(999))]

        # Mock _build_adapter_from_row to return a new mock
        new_adapter = _mock_adapter("mqtt")
        with patch.object(orch, "_build_adapter_from_row", return_value=new_adapter):
            result = await orch.restart_connector(1)

        assert result is True
        old_adapter.stop.assert_awaited_once()
        assert orch._adapters[0] is new_adapter

    async def test_disabled_connector_removes_adapter(self) -> None:
        config = _make_config()
        store = _mock_store(
            connector_row={
                "id": 1, "type": "mqtt", "name": "mqtt-primary",
                "enabled": False, "config": {},
            },
            config=config,
        )

        orch = Orchestrator(config, config_store=store)
        orch._build_components()

        old_adapter = _mock_adapter("mqtt")
        orch._adapters = [old_adapter]
        orch._adapter_tasks = [asyncio.create_task(asyncio.sleep(999))]

        result = await orch.restart_connector(1)
        assert result is True
        old_adapter.stop.assert_awaited_once()
        assert len(orch._adapters) == 0


# ---------------------------------------------------------------------------
# Tests: restart_service
# ---------------------------------------------------------------------------


class TestRestartService:
    async def test_no_config_store_returns_false(self) -> None:
        orch = Orchestrator(_make_config())
        orch._build_components()
        result = await orch.restart_service(1)
        assert result is False

    async def test_service_not_found_returns_false(self) -> None:
        store = _mock_store(service_row=None)
        orch = Orchestrator(_make_config(), config_store=store)
        orch._build_components()
        result = await orch.restart_service(999)
        assert result is False

    async def test_non_handler_service_returns_false(self) -> None:
        """Internal service types (influxdb, llm_triage, etc) cannot be restarted."""
        store = _mock_store(
            service_row={
                "id": 1, "type": "influxdb", "name": "influxdb-primary",
                "enabled": True, "config": {},
            },
        )
        orch = Orchestrator(_make_config(), config_store=store)
        orch._build_components()
        result = await orch.restart_service(1)
        assert result is False

    async def test_stops_old_handler_and_starts_new(self) -> None:
        config = _make_config()
        store = _mock_store(
            service_row={
                "id": 1, "type": "ha_handler", "name": "ha-primary",
                "enabled": True, "config": {},
            },
            config=config,
        )

        orch = Orchestrator(config, config_store=store)
        orch._build_components()

        # Inject a mock handler
        old_handler = _mock_handler("homeassistant")
        orch._handlers["homeassistant"] = old_handler

        new_handler = _mock_handler("homeassistant")
        with patch.object(orch, "_build_handler", return_value=new_handler):
            result = await orch.restart_service(1)

        assert result is True
        old_handler.stop.assert_awaited_once()
        new_handler.start.assert_awaited_once()
        assert orch._handlers["homeassistant"] is new_handler

    async def test_disabled_service_removes_handler(self) -> None:
        config = _make_config()
        store = _mock_store(
            service_row={
                "id": 1, "type": "ha_handler", "name": "ha-primary",
                "enabled": False, "config": {},
            },
            config=config,
        )

        orch = Orchestrator(config, config_store=store)
        orch._build_components()

        old_handler = _mock_handler("homeassistant")
        orch._handlers["homeassistant"] = old_handler

        result = await orch.restart_service(1)
        assert result is True
        old_handler.stop.assert_awaited_once()
        assert "homeassistant" not in orch._handlers
