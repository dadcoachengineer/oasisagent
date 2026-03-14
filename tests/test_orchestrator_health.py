"""Tests for Orchestrator.get_component_health()."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, PropertyMock

from oasisagent.llm.client import LLMRole
from oasisagent.orchestrator import Orchestrator

if TYPE_CHECKING:
    from oasisagent.config import OasisAgentConfig

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


def _mock_llm_client(
    *,
    triage_health: str = "unknown",
    reasoning_health: str = "unknown",
) -> MagicMock:
    """Create a mock LLMClient with configurable per-role health."""
    client = MagicMock()
    health_map = {
        LLMRole.TRIAGE: triage_health,
        LLMRole.REASONING: reasoning_health,
    }
    client.get_role_health.side_effect = lambda role: health_map[role]
    return client


def _make_orchestrator(
    adapters: list[MagicMock] | None = None,
    handlers: dict[str, MagicMock] | None = None,
    channels: list[MagicMock] | None = None,
    llm_client: MagicMock | None = None,
) -> Orchestrator:
    """Create an Orchestrator with mocked components (skip __init__)."""
    orch = object.__new__(Orchestrator)
    orch._adapters = adapters or []
    orch._handlers = handlers or {}
    orch._llm_client = llm_client
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

    async def test_unifi_handler_maps_to_db_type(self) -> None:
        handler = _mock_handler("unifi", healthy=True)
        orch = _make_orchestrator(handlers={"unifi": handler})
        result = await orch.get_component_health()
        assert result["services"]["unifi_handler"] == "connected"

    async def test_cloudflare_handler_maps_to_db_type(self) -> None:
        handler = _mock_handler("cloudflare", healthy=True)
        orch = _make_orchestrator(handlers={"cloudflare": handler})
        result = await orch.get_component_health()
        assert result["services"]["cloudflare_handler"] == "connected"

    async def test_unknown_handler_name_uses_raw_name(self) -> None:
        handler = _mock_handler("custom_thing", healthy=True)
        orch = _make_orchestrator(handlers={"custom_thing": handler})
        result = await orch.get_component_health()
        assert result["services"]["custom_thing"] == "connected"


class TestLLMHealth:
    async def test_no_llm_client_omits_llm_services(self) -> None:
        orch = _make_orchestrator(llm_client=None)
        result = await orch.get_component_health()
        assert "llm_triage" not in result["services"]
        assert "llm_reasoning" not in result["services"]

    async def test_no_calls_yet_returns_unknown(self) -> None:
        client = _mock_llm_client(triage_health="unknown", reasoning_health="unknown")
        orch = _make_orchestrator(llm_client=client)
        result = await orch.get_component_health()
        assert result["services"]["llm_triage"] == "unknown"
        assert result["services"]["llm_reasoning"] == "unknown"

    async def test_successful_call_returns_connected(self) -> None:
        client = _mock_llm_client(triage_health="connected", reasoning_health="connected")
        orch = _make_orchestrator(llm_client=client)
        result = await orch.get_component_health()
        assert result["services"]["llm_triage"] == "connected"
        assert result["services"]["llm_reasoning"] == "connected"

    async def test_failed_call_returns_error(self) -> None:
        client = _mock_llm_client(triage_health="error", reasoning_health="connected")
        orch = _make_orchestrator(llm_client=client)
        result = await orch.get_component_health()
        assert result["services"]["llm_triage"] == "error"
        assert result["services"]["llm_reasoning"] == "connected"


class TestInternalServices:
    async def test_internal_services_report_unknown(self) -> None:
        orch = _make_orchestrator()
        result = await orch.get_component_health()
        for svc in ("influxdb", "guardrails", "circuit_breaker", "llm_options"):
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


class TestBuildComponentsHandlerWiring:
    """Verify _build_components instantiates handlers when enabled in config."""

    @staticmethod
    def _make_config(
        **handler_overrides: dict[str, Any],
    ) -> OasisAgentConfig:
        from oasisagent.config import OasisAgentConfig

        handlers_dict: dict[str, Any] = {}
        for key, val in handler_overrides.items():
            handlers_dict[key] = {**val, "enabled": True}

        return OasisAgentConfig(handlers=handlers_dict)

    def test_unifi_handler_wired_when_enabled(self) -> None:
        config = self._make_config(unifi={
            "url": "https://192.168.1.1",
            "username": "admin",
            "password": "secret",
        })
        orch = Orchestrator(config)
        orch._build_components()
        assert "unifi" in orch._handlers
        assert orch._handlers["unifi"].name() == "unifi"

    def test_cloudflare_handler_wired_when_enabled(self) -> None:
        config = self._make_config(cloudflare={
            "api_token": "cf-tok-123",
        })
        orch = Orchestrator(config)
        orch._build_components()
        assert "cloudflare" in orch._handlers
        assert orch._handlers["cloudflare"].name() == "cloudflare"

    def test_handlers_not_wired_when_disabled(self) -> None:
        from oasisagent.config import OasisAgentConfig

        config = OasisAgentConfig()  # All handlers disabled by default
        orch = Orchestrator(config)
        orch._build_components()
        assert "unifi" not in orch._handlers
        assert "cloudflare" not in orch._handlers


class TestEmptyOrchestrator:
    async def test_empty_orchestrator(self) -> None:
        orch = _make_orchestrator()
        result = await orch.get_component_health()
        assert result["connectors"] == {}
        assert result["notifications"] == {}
        # Internal services always present, LLM omitted when no client
        assert "influxdb" in result["services"]
        assert "llm_options" in result["services"]
        assert "llm_triage" not in result["services"]
