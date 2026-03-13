"""Tests for Orchestrator.get_component_health()."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, PropertyMock

from oasisagent.orchestrator import Orchestrator

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_adapter(name: str, *, healthy: bool | Exception = True) -> MagicMock:
    """Create a mock IngestAdapter with configurable healthy()."""
    adapter = MagicMock()
    type(adapter).name = PropertyMock(return_value=name)
    if isinstance(healthy, Exception):
        adapter.healthy = AsyncMock(side_effect=healthy)
    else:
        adapter.healthy = AsyncMock(return_value=healthy)
    return adapter


def _mock_handler(name: str, *, healthy: bool | Exception = True) -> MagicMock:
    """Create a mock Handler with configurable healthy()."""
    handler = MagicMock()
    handler.name.return_value = name
    if isinstance(healthy, Exception):
        handler.healthy = AsyncMock(side_effect=healthy)
    else:
        handler.healthy = AsyncMock(return_value=healthy)
    return handler


def _mock_channel(name: str, *, healthy: bool | Exception = True) -> MagicMock:
    """Create a mock NotificationChannel with configurable healthy()."""
    channel = MagicMock()
    channel.name.return_value = name
    if isinstance(healthy, Exception):
        channel.healthy = AsyncMock(side_effect=healthy)
    else:
        channel.healthy = AsyncMock(return_value=healthy)
    return channel


def _make_orchestrator(
    adapters: list[MagicMock] | None = None,
    handlers: dict[str, MagicMock] | None = None,
    channels: list[MagicMock] | None = None,
) -> Orchestrator:
    """Create an Orchestrator with mocked components (skip __init__)."""
    orch = object.__new__(Orchestrator)
    orch._adapters = adapters or []
    orch._handlers = handlers or {}
    if channels is not None:
        dispatcher = MagicMock()
        type(dispatcher).channels = PropertyMock(return_value=channels)
        orch._dispatcher = dispatcher
    else:
        orch._dispatcher = None
    return orch


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestConnectorHealth:
    async def test_healthy_adapter_returns_connected(self) -> None:
        adapter = _mock_adapter("mqtt", healthy=True)
        orch = _make_orchestrator(adapters=[adapter])
        result = await orch.get_component_health()
        assert result["connectors"]["mqtt"] == "connected"

    async def test_unhealthy_adapter_returns_disconnected(self) -> None:
        adapter = _mock_adapter("ha_websocket", healthy=False)
        orch = _make_orchestrator(adapters=[adapter])
        result = await orch.get_component_health()
        assert result["connectors"]["ha_websocket"] == "disconnected"

    async def test_errored_adapter_returns_error(self) -> None:
        adapter = _mock_adapter("mqtt", healthy=RuntimeError("boom"))
        orch = _make_orchestrator(adapters=[adapter])
        result = await orch.get_component_health()
        assert result["connectors"]["mqtt"] == "error"


class TestHandlerHealth:
    async def test_handler_maps_to_db_type(self) -> None:
        handler = _mock_handler("homeassistant", healthy=True)
        orch = _make_orchestrator(handlers={"homeassistant": handler})
        result = await orch.get_component_health()
        assert result["services"]["ha_handler"] == "connected"

    async def test_unhealthy_handler(self) -> None:
        handler = _mock_handler("docker", healthy=False)
        orch = _make_orchestrator(handlers={"docker": handler})
        result = await orch.get_component_health()
        assert result["services"]["docker_handler"] == "disconnected"

    async def test_errored_handler(self) -> None:
        handler = _mock_handler("portainer", healthy=ConnectionError("refused"))
        orch = _make_orchestrator(handlers={"portainer": handler})
        result = await orch.get_component_health()
        assert result["services"]["portainer_handler"] == "error"

    async def test_unknown_handler_name_uses_raw_name(self) -> None:
        handler = _mock_handler("custom_thing", healthy=True)
        orch = _make_orchestrator(handlers={"custom_thing": handler})
        result = await orch.get_component_health()
        assert result["services"]["custom_thing"] == "connected"


class TestInternalServices:
    async def test_internal_services_report_unknown(self) -> None:
        orch = _make_orchestrator()
        result = await orch.get_component_health()
        for svc in (
            "llm_triage", "llm_reasoning", "llm_options",
            "influxdb", "guardrails", "circuit_breaker",
        ):
            assert result["services"][svc] == "unknown"


class TestScannerAggregate:
    async def test_all_healthy_scanners(self) -> None:
        scanners = [
            _mock_adapter("scanner.cert", healthy=True),
            _mock_adapter("scanner.disk", healthy=True),
        ]
        orch = _make_orchestrator(adapters=scanners)
        result = await orch.get_component_health()
        assert result["services"]["scanner"] == "connected"
        assert "connectors" in result
        assert "scanner.cert" not in result["connectors"]
        assert result["scanner_detail"]["detail"] == "2/2 scanners healthy"

    async def test_one_unhealthy_scanner(self) -> None:
        scanners = [
            _mock_adapter("scanner.cert", healthy=True),
            _mock_adapter("scanner.disk", healthy=False),
        ]
        orch = _make_orchestrator(adapters=scanners)
        result = await orch.get_component_health()
        assert result["services"]["scanner"] == "disconnected"
        assert result["scanner_detail"]["detail"] == "1/2 scanners healthy"

    async def test_one_errored_scanner(self) -> None:
        scanners = [
            _mock_adapter("scanner.cert", healthy=True),
            _mock_adapter("scanner.disk", healthy=RuntimeError("fail")),
        ]
        orch = _make_orchestrator(adapters=scanners)
        result = await orch.get_component_health()
        assert result["services"]["scanner"] == "error"

    async def test_no_scanners_omits_scanner_key(self) -> None:
        adapter = _mock_adapter("mqtt", healthy=True)
        orch = _make_orchestrator(adapters=[adapter])
        result = await orch.get_component_health()
        assert "scanner" not in result["services"]
        assert "scanner_detail" not in result


class TestNotificationHealth:
    async def test_healthy_channel(self) -> None:
        channel = _mock_channel("mqtt", healthy=True)
        orch = _make_orchestrator(channels=[channel])
        result = await orch.get_component_health()
        assert result["notifications"]["mqtt_notification"] == "connected"

    async def test_unhealthy_channel(self) -> None:
        channel = _mock_channel("mqtt", healthy=False)
        orch = _make_orchestrator(channels=[channel])
        result = await orch.get_component_health()
        assert result["notifications"]["mqtt_notification"] == "disconnected"

    async def test_no_dispatcher(self) -> None:
        orch = _make_orchestrator(channels=None)
        result = await orch.get_component_health()
        assert result["notifications"] == {}


class TestEmptyOrchestrator:
    async def test_empty_orchestrator(self) -> None:
        orch = _make_orchestrator()
        result = await orch.get_component_health()
        assert result["connectors"] == {}
        assert result["notifications"] == {}
        # Internal services always present
        assert "llm_triage" in result["services"]
