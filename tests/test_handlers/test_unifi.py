"""Tests for the UniFi Network handler."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest

from oasisagent.config import UnifiHandlerConfig
from oasisagent.handlers.unifi import UnifiHandler
from oasisagent.models import (
    ActionResult,
    ActionStatus,
    Event,
    EventMetadata,
    RecommendedAction,
    RiskTier,
    Severity,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(**overrides: Any) -> UnifiHandlerConfig:
    defaults: dict[str, Any] = {
        "enabled": True,
        "url": "https://192.168.1.1",
        "username": "admin",
        "password": "secret",
        "site": "default",
        "is_udm": True,
        "verify_ssl": False,
        "timeout": 10,
        "verify_timeout": 5,
        "verify_poll_interval": 0.1,
    }
    defaults.update(overrides)
    return UnifiHandlerConfig(**defaults)


def _make_event(**overrides: Any) -> Event:
    defaults: dict[str, Any] = {
        "source": "unifi",
        "system": "unifi",
        "event_type": "device_disconnected",
        "entity_id": "aa:bb:cc:dd:ee:ff",
        "severity": Severity.ERROR,
        "timestamp": datetime.now(UTC),
        "payload": {},
        "metadata": EventMetadata(),
    }
    defaults.update(overrides)
    return Event(**defaults)


def _make_action(**overrides: Any) -> RecommendedAction:
    defaults: dict[str, Any] = {
        "description": "Test action",
        "handler": "unifi",
        "operation": "notify",
        "params": {},
        "risk_tier": RiskTier.RECOMMEND,
    }
    defaults.update(overrides)
    return RecommendedAction(**defaults)


def _make_handler(**overrides: Any) -> UnifiHandler:
    """Create a handler with a mocked UnifiClient."""
    config = _make_config(**overrides)
    handler = UnifiHandler(config)
    # Mock the client so we don't need real connections
    mock_client = AsyncMock()
    handler._client = mock_client
    return handler


# ---------------------------------------------------------------------------
# Identity & lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_name(self) -> None:
        handler = UnifiHandler(_make_config())
        assert handler.name() == "unifi"

    @pytest.mark.asyncio
    async def test_start_creates_client(self) -> None:
        with patch("oasisagent.handlers.unifi.UnifiClient") as mock_cls:
            mock_client = AsyncMock()
            mock_cls.return_value = mock_client

            handler = UnifiHandler(_make_config())
            await handler.start()

            mock_client.connect.assert_called_once()
            assert handler._client is mock_client

    @pytest.mark.asyncio
    async def test_stop_closes_client(self) -> None:
        handler = _make_handler()
        mock_client = handler._client
        await handler.stop()

        mock_client.close.assert_called_once()
        assert handler._client is None

    @pytest.mark.asyncio
    async def test_stop_when_not_started(self) -> None:
        handler = UnifiHandler(_make_config())
        await handler.stop()  # should not raise

    @pytest.mark.asyncio
    async def test_ensure_started_raises(self) -> None:
        handler = UnifiHandler(_make_config())
        with pytest.raises(RuntimeError, match="start\\(\\) must be called"):
            await handler.execute(_make_event(), _make_action())


# ---------------------------------------------------------------------------
# healthy
# ---------------------------------------------------------------------------


class TestHealthy:
    @pytest.mark.asyncio
    async def test_healthy_no_client(self) -> None:
        handler = UnifiHandler(_make_config())
        assert await handler.healthy() is False

    @pytest.mark.asyncio
    async def test_healthy_success(self) -> None:
        handler = _make_handler()
        handler._client.get = AsyncMock(return_value={
            "data": [{"subsystem": "wlan", "status": "ok"}],
        })

        assert await handler.healthy() is True
        handler._client.get.assert_called_once_with("stat/health")

    @pytest.mark.asyncio
    async def test_healthy_http_error(self) -> None:
        handler = _make_handler()
        handler._client.get = AsyncMock(
            side_effect=aiohttp.ClientError("connection refused"),
        )

        assert await handler.healthy() is False

    @pytest.mark.asyncio
    async def test_healthy_timeout(self) -> None:
        async def _slow_get(_endpoint: str) -> dict:
            await asyncio.sleep(10)
            return {"data": []}

        handler = _make_handler()
        handler._client.get = _slow_get
        handler._HEALTH_TIMEOUT = 0.1

        assert await handler.healthy() is False

    @pytest.mark.asyncio
    async def test_healthy_auth_failure(self) -> None:
        handler = _make_handler()
        handler._client.get = AsyncMock(
            side_effect=ConnectionError("auth failed"),
        )

        assert await handler.healthy() is False


# ---------------------------------------------------------------------------
# can_handle
# ---------------------------------------------------------------------------


class TestCanHandle:
    @pytest.mark.asyncio
    async def test_known_operations(self) -> None:
        handler = _make_handler()
        for op in ("notify", "restart_device", "block_client", "unblock_client"):
            action = _make_action(operation=op)
            assert await handler.can_handle(_make_event(), action)

    @pytest.mark.asyncio
    async def test_unknown_operation(self) -> None:
        handler = _make_handler()
        action = _make_action(operation="delete_everything")
        assert not await handler.can_handle(_make_event(), action)

    @pytest.mark.asyncio
    async def test_wrong_handler(self) -> None:
        handler = _make_handler()
        action = _make_action(handler="docker")
        assert not await handler.can_handle(_make_event(), action)


# ---------------------------------------------------------------------------
# notify operation
# ---------------------------------------------------------------------------


class TestNotifyOp:
    @pytest.mark.asyncio
    async def test_notify_success(self) -> None:
        handler = _make_handler()
        event = _make_event()
        action = _make_action(
            operation="notify",
            params={"message": "Device is offline"},
        )

        result = await handler.execute(event, action)

        assert result.status == ActionStatus.SUCCESS
        assert result.details["message"] == "Device is offline"
        assert result.details["entity_id"] == "aa:bb:cc:dd:ee:ff"
        assert result.duration_ms is not None

    @pytest.mark.asyncio
    async def test_notify_uses_description_as_fallback(self) -> None:
        handler = _make_handler()
        action = _make_action(
            operation="notify",
            description="AP disconnected",
            params={},
        )

        result = await handler.execute(_make_event(), action)

        assert result.details["message"] == "AP disconnected"


# ---------------------------------------------------------------------------
# restart_device operation
# ---------------------------------------------------------------------------


class TestRestartDeviceOp:
    @pytest.mark.asyncio
    async def test_restart_device_success(self) -> None:
        handler = _make_handler()
        handler._client.post = AsyncMock(return_value={"meta": {"rc": "ok"}})

        action = _make_action(
            operation="restart_device",
            params={"mac": "aa:bb:cc:dd:ee:ff"},
        )

        result = await handler.execute(_make_event(), action)

        assert result.status == ActionStatus.SUCCESS
        assert result.details["mac"] == "aa:bb:cc:dd:ee:ff"
        handler._client.post.assert_called_once_with(
            "cmd/devmgr",
            {"cmd": "restart", "mac": "aa:bb:cc:dd:ee:ff"},
        )

    @pytest.mark.asyncio
    async def test_restart_device_uses_entity_id_fallback(self) -> None:
        handler = _make_handler()
        handler._client.post = AsyncMock(return_value={"meta": {"rc": "ok"}})

        event = _make_event(entity_id="11:22:33:44:55:66")
        action = _make_action(operation="restart_device", params={})

        result = await handler.execute(event, action)

        assert result.status == ActionStatus.SUCCESS
        assert result.details["mac"] == "11:22:33:44:55:66"

    @pytest.mark.asyncio
    async def test_restart_device_no_mac_fails(self) -> None:
        handler = _make_handler()
        event = _make_event(entity_id="")
        action = _make_action(operation="restart_device", params={})

        result = await handler.execute(event, action)

        assert result.status == ActionStatus.FAILURE
        assert "mac" in result.error_message

    @pytest.mark.asyncio
    async def test_restart_device_http_error(self) -> None:
        handler = _make_handler()
        handler._client.post = AsyncMock(
            side_effect=aiohttp.ClientError("connection refused"),
        )

        action = _make_action(
            operation="restart_device",
            params={"mac": "aa:bb:cc"},
        )

        result = await handler.execute(_make_event(), action)

        assert result.status == ActionStatus.FAILURE
        assert "HTTP error" in result.error_message
        assert result.duration_ms is not None


# ---------------------------------------------------------------------------
# block_client / unblock_client operations
# ---------------------------------------------------------------------------


class TestBlockClientOps:
    @pytest.mark.asyncio
    async def test_block_client_success(self) -> None:
        handler = _make_handler()
        handler._client.post = AsyncMock(return_value={"meta": {"rc": "ok"}})

        action = _make_action(
            operation="block_client",
            params={"mac": "dd:ee:ff:00:11:22"},
        )

        result = await handler.execute(_make_event(), action)

        assert result.status == ActionStatus.SUCCESS
        handler._client.post.assert_called_once_with(
            "cmd/stamgr",
            {"cmd": "block-sta", "mac": "dd:ee:ff:00:11:22"},
        )

    @pytest.mark.asyncio
    async def test_block_client_no_mac_fails(self) -> None:
        handler = _make_handler()
        action = _make_action(operation="block_client", params={})

        result = await handler.execute(_make_event(), action)

        assert result.status == ActionStatus.FAILURE
        assert "mac" in result.error_message

    @pytest.mark.asyncio
    async def test_unblock_client_success(self) -> None:
        handler = _make_handler()
        handler._client.post = AsyncMock(return_value={"meta": {"rc": "ok"}})

        action = _make_action(
            operation="unblock_client",
            params={"mac": "dd:ee:ff:00:11:22"},
        )

        result = await handler.execute(_make_event(), action)

        assert result.status == ActionStatus.SUCCESS
        handler._client.post.assert_called_once_with(
            "cmd/stamgr",
            {"cmd": "unblock-sta", "mac": "dd:ee:ff:00:11:22"},
        )

    @pytest.mark.asyncio
    async def test_unblock_client_no_mac_fails(self) -> None:
        handler = _make_handler()
        action = _make_action(operation="unblock_client", params={})

        result = await handler.execute(_make_event(), action)

        assert result.status == ActionStatus.FAILURE


# ---------------------------------------------------------------------------
# Unknown operation
# ---------------------------------------------------------------------------


class TestUnknownOp:
    @pytest.mark.asyncio
    async def test_unknown_operation_returns_failure(self) -> None:
        handler = _make_handler()
        action = _make_action(operation="delete_site")

        result = await handler.execute(_make_event(), action)

        assert result.status == ActionStatus.FAILURE
        assert "Unknown operation" in result.error_message


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------


class TestVerify:
    @pytest.mark.asyncio
    async def test_verify_non_restart_returns_true(self) -> None:
        handler = _make_handler()
        action = _make_action(operation="notify")
        result = ActionResult(status=ActionStatus.SUCCESS)

        verify = await handler.verify(_make_event(), action, result)

        assert verify.verified is True

    @pytest.mark.asyncio
    async def test_verify_restart_device_recovered(self) -> None:
        handler = _make_handler()
        handler._client.get = AsyncMock(return_value={
            "data": [{"mac": "aa:bb:cc:dd:ee:ff", "state": 1, "name": "AP-1"}],
        })

        action = _make_action(
            operation="restart_device",
            params={"mac": "aa:bb:cc:dd:ee:ff"},
        )
        exec_result = ActionResult(status=ActionStatus.SUCCESS)

        verify = await handler.verify(_make_event(), action, exec_result)

        assert verify.verified is True
        assert "recovered" in verify.message

    @pytest.mark.asyncio
    async def test_verify_restart_device_not_recovered(self) -> None:
        handler = _make_handler(verify_timeout=1, verify_poll_interval=0.1)
        handler._client.get = AsyncMock(return_value={
            "data": [{"mac": "aa:bb:cc:dd:ee:ff", "state": 0}],
        })

        action = _make_action(
            operation="restart_device",
            params={"mac": "aa:bb:cc:dd:ee:ff"},
        )
        exec_result = ActionResult(status=ActionStatus.SUCCESS)

        verify = await handler.verify(_make_event(), action, exec_result)

        assert verify.verified is False
        assert "did not recover" in verify.message

    @pytest.mark.asyncio
    async def test_verify_restart_survives_http_error(self) -> None:
        """HTTP errors during verification should not crash, just keep polling."""
        handler = _make_handler(verify_timeout=1, verify_poll_interval=0.1)
        # First call errors, second returns recovered
        handler._client.get = AsyncMock(side_effect=[
            aiohttp.ClientError("timeout"),
            {"data": [{"mac": "aa:bb:cc:dd:ee:ff", "state": 1, "name": "AP-1"}]},
        ])

        action = _make_action(
            operation="restart_device",
            params={"mac": "aa:bb:cc:dd:ee:ff"},
        )
        exec_result = ActionResult(status=ActionStatus.SUCCESS)

        verify = await handler.verify(_make_event(), action, exec_result)

        assert verify.verified is True


# ---------------------------------------------------------------------------
# get_context
# ---------------------------------------------------------------------------


class TestGetContext:
    @pytest.mark.asyncio
    async def test_get_context_device_found(self) -> None:
        handler = _make_handler()
        handler._client.get = AsyncMock(side_effect=[
            # stat/device
            {"data": [{
                "mac": "aa:bb:cc:dd:ee:ff",
                "name": "AP-Living-Room",
                "type": "uap",
                "model": "U6-Lite",
                "version": "6.5.0",
                "state": 1,
                "uptime": 86400,
                "system-stats": {"cpu": "15.0", "mem": "40.0"},
            }]},
            # stat/health
            {"data": [
                {"subsystem": "wlan", "status": "ok"},
                {"subsystem": "wan", "status": "ok"},
            ]},
        ])

        context = await handler.get_context(_make_event())

        assert context["device"]["name"] == "AP-Living-Room"
        assert context["device"]["model"] == "U6-Lite"
        assert context["health"]["wlan"] == "ok"
        assert context["health"]["wan"] == "ok"

    @pytest.mark.asyncio
    async def test_get_context_device_not_found(self) -> None:
        handler = _make_handler()
        handler._client.get = AsyncMock(side_effect=[
            {"data": [{"mac": "other:mac", "name": "Other"}]},
            {"data": []},
        ])

        context = await handler.get_context(_make_event())

        assert "device" not in context
        assert context["health"] == {}

    @pytest.mark.asyncio
    async def test_get_context_device_error(self) -> None:
        handler = _make_handler()
        handler._client.get = AsyncMock(side_effect=[
            aiohttp.ClientError("connection refused"),
            {"data": [{"subsystem": "wlan", "status": "ok"}]},
        ])

        context = await handler.get_context(_make_event())

        assert "device_error" in context
        assert context["health"]["wlan"] == "ok"

    @pytest.mark.asyncio
    async def test_get_context_health_error(self) -> None:
        handler = _make_handler()
        handler._client.get = AsyncMock(side_effect=[
            {"data": [{"mac": "aa:bb:cc:dd:ee:ff", "name": "AP", "type": "uap",
                       "model": "", "version": "", "state": 1, "uptime": 0,
                       "system-stats": {}}]},
            aiohttp.ClientError("timeout"),
        ])

        context = await handler.get_context(_make_event())

        assert "device" in context
        assert "health_error" in context


# ---------------------------------------------------------------------------
# Config & registry
# ---------------------------------------------------------------------------


class TestConfigAndRegistry:
    def test_config_defaults(self) -> None:
        config = UnifiHandlerConfig()
        assert config.enabled is False
        assert config.is_udm is True
        assert config.verify_timeout == 30

    def test_config_extra_forbidden(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="Extra inputs"):
            _make_config(bogus="nope")

    def test_handlers_config_has_unifi(self) -> None:
        from oasisagent.config import HandlersConfig

        config = HandlersConfig()
        assert hasattr(config, "unifi")
        assert isinstance(config.unifi, UnifiHandlerConfig)

    def test_registry_entry(self) -> None:
        from oasisagent.db.registry import CORE_SERVICE_TYPES

        assert "unifi_handler" in CORE_SERVICE_TYPES
        meta = CORE_SERVICE_TYPES["unifi_handler"]
        assert meta.model is UnifiHandlerConfig
        assert "password" in meta.secret_fields

    def test_handler_exported_from_init(self) -> None:
        from oasisagent.handlers import UnifiHandler as Exported

        assert Exported is UnifiHandler
