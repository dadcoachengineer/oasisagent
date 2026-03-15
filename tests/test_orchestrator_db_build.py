"""Tests for DB-first component building in the Orchestrator.

Covers ``_build_infrastructure()``, ``_build_db_components()``,
``_build_components_from_config()`` fallback, and the from_row builder
methods.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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
from oasisagent.db.registry import CONNECTOR_TYPES, CORE_SERVICE_TYPES, NOTIFICATION_TYPES
from oasisagent.orchestrator import Orchestrator

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(**agent_overrides: Any) -> OasisAgentConfig:
    """Create a minimal config for testing. All external services disabled."""
    agent_defaults: dict[str, Any] = {
        "event_queue_size": 100,
        "shutdown_timeout": 2,
        "event_ttl": 300,
        "known_fixes_dir": "/nonexistent",
    }
    agent_defaults.update(agent_overrides)
    return OasisAgentConfig(
        agent=AgentConfig(**agent_defaults),
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


def _mock_store(
    *,
    connectors: list[dict[str, Any]] | None = None,
    services: list[dict[str, Any]] | None = None,
    notifications: list[dict[str, Any]] | None = None,
) -> MagicMock:
    """Create a mock ConfigStore with list_* methods."""
    store = MagicMock()
    store.list_connectors = AsyncMock(return_value=connectors or [])
    store.list_services = AsyncMock(return_value=services or [])
    store.list_notifications = AsyncMock(return_value=notifications or [])
    return store


def _connector_row(
    row_id: int, db_type: str, config: dict[str, Any],
    *, enabled: bool = True, name: str = "",
) -> dict[str, Any]:
    return {
        "id": row_id, "type": db_type,
        "name": name or db_type,
        "enabled": enabled, "config": config,
        "created_at": "2026-01-01", "updated_at": "2026-01-01",
    }


def _service_row(
    row_id: int, db_type: str, config: dict[str, Any],
    *, enabled: bool = True, name: str = "",
) -> dict[str, Any]:
    return {
        "id": row_id, "type": db_type,
        "name": name or db_type,
        "enabled": enabled, "config": config,
        "created_at": "2026-01-01", "updated_at": "2026-01-01",
    }


def _notification_row(
    row_id: int, db_type: str, config: dict[str, Any],
    *, enabled: bool = True, name: str = "",
) -> dict[str, Any]:
    return {
        "id": row_id, "type": db_type,
        "name": name or db_type,
        "enabled": enabled, "config": config,
        "created_at": "2026-01-01", "updated_at": "2026-01-01",
    }


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestBuildInfrastructure:
    def test_creates_all_singletons(self) -> None:
        orch = Orchestrator(_make_config())
        orch._build_infrastructure()

        assert orch._queue is not None
        assert orch._correlator is not None
        assert orch._registry is not None
        assert orch._circuit_breaker is not None
        assert orch._guardrails is not None
        assert orch._llm_client is not None
        assert orch._triage_service is not None
        assert orch._reasoning_service is not None
        assert orch._decision_engine is not None
        assert orch._audit is not None
        assert orch._pending_queue is not None

    def test_does_not_build_user_components(self) -> None:
        orch = Orchestrator(_make_config())
        orch._build_infrastructure()

        assert orch._handlers == {}
        assert orch._adapters == []
        assert orch._dispatcher is None


class TestBuildDbComponents:
    async def test_builds_adapters_from_rows(self) -> None:
        mock_adapter = MagicMock()
        store = _mock_store(
            connectors=[
                _connector_row(1, "mqtt", {
                    "host": "localhost", "port": 1883, "topics": ["test/#"],
                }),
            ],
        )
        orch = Orchestrator(_make_config(), config_store=store)
        orch._build_infrastructure()

        with patch.object(orch, "_build_adapter_from_row", return_value=mock_adapter):
            await orch._build_db_components()

        assert mock_adapter in orch._adapters

    async def test_builds_handlers_from_rows(self) -> None:
        mock_handler = MagicMock()
        mock_handler.name.return_value = "homeassistant"
        store = _mock_store(
            services=[
                _service_row(1, "ha_handler", {
                    "url": "http://ha:8123", "token": "test",
                }),
            ],
        )
        orch = Orchestrator(_make_config(), config_store=store)
        orch._build_infrastructure()

        with patch.object(orch, "_build_handler_from_row", return_value=mock_handler):
            await orch._build_db_components()

        assert orch._handlers["homeassistant"] is mock_handler

    async def test_builds_notifications_from_rows(self) -> None:
        mock_channel = MagicMock()
        store = _mock_store(
            notifications=[
                _notification_row(1, "email", {
                    "smtp_host": "mail.test", "smtp_port": 587,
                    "from_address": "a@b.c", "to_addresses": ["x@y.z"],
                    "username": "u", "password": "p",
                }),
            ],
        )
        orch = Orchestrator(_make_config(), config_store=store)
        orch._build_infrastructure()

        with patch.object(orch, "_build_notification_from_row", return_value=mock_channel):
            await orch._build_db_components()

        assert orch._dispatcher is not None
        assert mock_channel in orch._dispatcher.channels

    async def test_builds_dispatcher_even_with_no_channels(self) -> None:
        store = _mock_store()
        orch = Orchestrator(_make_config(), config_store=store)
        orch._build_infrastructure()
        await orch._build_db_components()

        assert orch._dispatcher is not None
        assert orch._dispatcher.channels == []


class TestStartupSequence:
    async def test_infrastructure_then_db(self) -> None:
        """start() calls infrastructure then db_components when store exists."""
        store = _mock_store()
        orch = Orchestrator(_make_config(), config_store=store)

        call_order: list[str] = []
        orig_infra = orch._build_infrastructure

        def tracked_infra() -> None:
            call_order.append("infrastructure")
            orig_infra()

        async def tracked_db() -> None:
            call_order.append("db_components")
            # Ensure dispatcher is created so _start_components works
            from oasisagent.notifications.dispatcher import NotificationDispatcher
            orch._dispatcher = NotificationDispatcher([])

        with (
            patch.object(orch, "_build_infrastructure", side_effect=tracked_infra),
            patch.object(orch, "_build_db_components", side_effect=tracked_db),
            patch.object(orch, "_start_components", new_callable=AsyncMock),
        ):
            await orch.start()

        assert call_order == ["infrastructure", "db_components"]

    async def test_fallback_to_config_when_no_store(self) -> None:
        orch = Orchestrator(_make_config())

        call_order: list[str] = []
        orig_infra = orch._build_infrastructure

        def tracked_infra() -> None:
            call_order.append("infrastructure")
            orig_infra()

        def tracked_config() -> None:
            call_order.append("config")
            # Need dispatcher for _start_components
            from oasisagent.notifications.dispatcher import NotificationDispatcher
            orch._dispatcher = NotificationDispatcher([])

        with (
            patch.object(orch, "_build_infrastructure", side_effect=tracked_infra),
            patch.object(orch, "_build_components_from_config", side_effect=tracked_config),
            patch.object(orch, "_start_components", new_callable=AsyncMock),
        ):
            await orch.start()

        assert call_order == ["infrastructure", "config"]


class TestFromRowBuilders:
    def test_adapter_from_row_uses_registry(self) -> None:
        orch = Orchestrator(_make_config())
        orch._build_infrastructure()

        adapter = orch._build_adapter_from_row("mqtt", {
            "broker": "mqtt://localhost:1883",
        })
        assert adapter is not None
        assert adapter.name == "mqtt"

    def test_handler_from_row_uses_registry(self) -> None:
        orch = Orchestrator(_make_config())
        orch._build_infrastructure()

        handler = orch._build_handler_from_row("ha_handler", {
            "url": "http://ha:8123", "token": "test-token",
        })
        assert handler is not None
        assert handler.name() == "homeassistant"

    def test_notification_from_row_uses_registry(self) -> None:
        orch = Orchestrator(_make_config())

        channel = orch._build_notification_from_row("mqtt_notification", {
            "broker": "mqtt://localhost:1883",
            "topic_prefix": "oasis/notifications",
        })
        assert channel is not None
        assert channel.name() == "mqtt"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestBadRowIsolation:
    async def test_bad_adapter_row_does_not_block_others(self) -> None:
        good_adapter = MagicMock()
        good_adapter.name.return_value = "good"

        def side_effect(db_type: str, config: dict[str, Any]) -> MagicMock | None:
            if db_type == "bad_type":
                raise ValueError("bad config")
            return good_adapter

        store = _mock_store(
            connectors=[
                _connector_row(1, "bad_type", {}),
                _connector_row(2, "mqtt", {"host": "localhost"}),
            ],
        )
        orch = Orchestrator(_make_config(), config_store=store)
        orch._build_infrastructure()

        with patch.object(orch, "_build_adapter_from_row", side_effect=side_effect):
            await orch._build_db_components()

        assert good_adapter in orch._adapters

    async def test_bad_handler_row_does_not_block_others(self) -> None:
        good_handler = MagicMock()
        good_handler.name.return_value = "docker"

        call_count = 0

        def side_effect(db_type: str, config: dict[str, Any]) -> MagicMock | None:
            nonlocal call_count
            call_count += 1
            if db_type == "ha_handler":
                raise ValueError("bad ha config")
            return good_handler

        store = _mock_store(
            services=[
                _service_row(1, "ha_handler", {}),
                _service_row(2, "docker_handler", {}),
            ],
        )
        orch = Orchestrator(_make_config(), config_store=store)
        orch._build_infrastructure()

        with patch.object(orch, "_build_handler_from_row", side_effect=side_effect):
            await orch._build_db_components()

        assert orch._handlers.get("docker") is good_handler


class TestHttpPollerAggregation:
    async def test_three_rows_one_adapter(self) -> None:
        store = _mock_store(
            connectors=[
                _connector_row(1, "http_poller", {
                    "url": "http://svc1/health", "system": "svc1",
                    "interval": 60,
                }),
                _connector_row(2, "http_poller", {
                    "url": "http://svc2/health", "system": "svc2",
                    "interval": 60,
                }),
                _connector_row(3, "http_poller", {
                    "url": "http://svc3/health", "system": "svc3",
                    "interval": 60,
                }),
            ],
        )
        orch = Orchestrator(_make_config(), config_store=store)
        orch._build_infrastructure()
        await orch._build_db_components()

        # Should be exactly one HttpPollerAdapter with 3 targets
        from oasisagent.ingestion.http_poller import HttpPollerAdapter
        http_adapters = [a for a in orch._adapters if isinstance(a, HttpPollerAdapter)]
        assert len(http_adapters) == 1
        assert len(http_adapters[0]._targets) == 3


class TestScannerHandlerCrossRef:
    async def test_missing_handler_skips_scanner(self, caplog: pytest.LogCaptureFixture) -> None:
        store = _mock_store(
            services=[
                _service_row(1, "scanner", {
                    "interval": 900,
                    "ha_health": {"enabled": True, "interval": 900},
                }),
                # No ha_handler row!
            ],
        )
        orch = Orchestrator(_make_config(), config_store=store)
        orch._build_infrastructure()

        with caplog.at_level(logging.WARNING):
            await orch._build_db_components()

        assert "no enabled ha_handler found" in caplog.text.lower()
        # No ha_health scanner should be built
        scanner_adapters = [a for a in orch._adapters if a.name.startswith("scanner.")]
        assert len(scanner_adapters) == 0


class TestEmptyDb:
    async def test_empty_tables_no_crash(self) -> None:
        store = _mock_store()
        orch = Orchestrator(_make_config(), config_store=store)
        orch._build_infrastructure()
        await orch._build_db_components()

        assert orch._adapters == []
        assert orch._handlers == {}
        assert orch._dispatcher is not None
        assert orch._dispatcher.channels == []

    async def test_all_rows_disabled_no_components(self) -> None:
        store = _mock_store(
            connectors=[
                _connector_row(1, "mqtt", {}, enabled=False),
            ],
            services=[
                _service_row(1, "ha_handler", {}, enabled=False),
            ],
            notifications=[
                _notification_row(1, "email", {}, enabled=False),
            ],
        )
        orch = Orchestrator(_make_config(), config_store=store)
        orch._build_infrastructure()
        await orch._build_db_components()

        assert orch._adapters == []
        assert orch._handlers == {}
        assert orch._dispatcher.channels == []


class TestMultipleMqttConnectors:
    async def test_first_used_for_approval_warning_logged(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        store = _mock_store(
            connectors=[
                _connector_row(1, "mqtt", {
                    "broker": "mqtt://broker1:1883",
                }),
                _connector_row(2, "mqtt", {
                    "broker": "mqtt://broker2:1883",
                }),
            ],
        )
        orch = Orchestrator(_make_config(), config_store=store)
        orch._build_infrastructure()

        with caplog.at_level(logging.WARNING):
            await orch._build_db_components()

        assert "multiple mqtt connectors" in caplog.text.lower()
        assert orch._approval_listener is not None


class TestUnknownTypes:
    def test_unknown_adapter_type_returns_none(self) -> None:
        orch = Orchestrator(_make_config())
        orch._build_infrastructure()

        result = orch._build_adapter_from_row("totally_fake_type", {})
        assert result is None

    def test_handler_from_row_non_handler_type_returns_none(self) -> None:
        orch = Orchestrator(_make_config())

        result = orch._build_handler_from_row("llm_triage", {
            "base_url": "http://x", "model": "y", "api_key": "z",
        })
        assert result is None

    def test_notification_from_row_unknown_type_returns_none(self) -> None:
        orch = Orchestrator(_make_config())

        result = orch._build_notification_from_row("nonexistent", {})
        assert result is None

    def test_webhook_receiver_adapter_returns_none(self) -> None:
        """webhook_receiver has no adapter class — returns None."""
        orch = Orchestrator(_make_config())
        orch._build_infrastructure()

        result = orch._build_adapter_from_row("webhook_receiver", {})
        assert result is None


class TestRegistryModulePaths:
    def test_all_handler_types_have_module_path(self) -> None:
        handler_types = {
            "ha_handler", "docker_handler", "portainer_handler",
            "proxmox_handler", "unifi_handler", "cloudflare_handler",
        }
        for db_type in handler_types:
            meta = CORE_SERVICE_TYPES[db_type]
            assert meta.module_path, f"{db_type} missing module_path"
            assert meta.class_name, f"{db_type} missing class_name"

    def test_infrastructure_types_have_no_module_path(self) -> None:
        infra_types = {
            "llm_triage", "llm_reasoning", "llm_options",
            "influxdb", "guardrails", "circuit_breaker", "scanner",
        }
        for db_type in infra_types:
            meta = CORE_SERVICE_TYPES[db_type]
            assert meta.module_path == "", f"{db_type} should not have module_path"

    def test_all_notification_types_have_module_path(self) -> None:
        for db_type, meta in NOTIFICATION_TYPES.items():
            assert meta.module_path, f"{db_type} missing module_path"
            assert meta.class_name, f"{db_type} missing class_name"

    def test_connector_types_with_adapters_have_module_path(self) -> None:
        for db_type, meta in CONNECTOR_TYPES.items():
            if db_type == "webhook_receiver":
                assert meta.module_path == ""
                continue
            assert meta.module_path, f"{db_type} missing module_path"
            assert meta.class_name, f"{db_type} missing class_name"
