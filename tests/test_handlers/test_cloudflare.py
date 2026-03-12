"""Tests for the Cloudflare handler."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from oasisagent.config import CloudflareHandlerConfig
from oasisagent.handlers.cloudflare import CloudflareHandler
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


def _make_config(**overrides: Any) -> CloudflareHandlerConfig:
    defaults: dict[str, Any] = {
        "enabled": True,
        "api_token": "test-token-123",
        "zone_id": "zone-abc",
        "account_id": "account-xyz",
        "timeout": 10,
    }
    defaults.update(overrides)
    return CloudflareHandlerConfig(**defaults)


def _make_event(**overrides: Any) -> Event:
    defaults: dict[str, Any] = {
        "source": "cloudflare",
        "system": "cloudflare",
        "event_type": "tunnel_disconnected",
        "entity_id": "my-tunnel",
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
        "handler": "cloudflare",
        "operation": "notify",
        "params": {},
        "risk_tier": RiskTier.RECOMMEND,
    }
    defaults.update(overrides)
    return RecommendedAction(**defaults)


def _mock_resp(
    json_data: dict[str, Any] | None = None,
) -> MagicMock:
    """Create a mock aiohttp response for async context managers."""
    resp = MagicMock()
    resp.status = 200
    resp.json = AsyncMock(
        return_value=json_data or {"success": True, "result": {}},
    )
    resp.request_info = MagicMock()
    resp.history = ()
    return resp


def _make_handler(**overrides: Any) -> CloudflareHandler:
    """Create a handler with a mocked CloudflareClient."""
    config = _make_config(**overrides)
    handler = CloudflareHandler(config)
    mock_client = AsyncMock()
    handler._client = mock_client
    return handler


# ---------------------------------------------------------------------------
# Identity & lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_name(self) -> None:
        handler = CloudflareHandler(_make_config())
        assert handler.name() == "cloudflare"

    @pytest.mark.asyncio
    async def test_start_creates_client(self) -> None:
        with patch("oasisagent.handlers.cloudflare.CloudflareClient") as mock_cls:
            mock_client = AsyncMock()
            mock_cls.return_value = mock_client

            handler = CloudflareHandler(_make_config())
            await handler.start()

            mock_client.start.assert_called_once()
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
        handler = CloudflareHandler(_make_config())
        await handler.stop()  # should not raise

    @pytest.mark.asyncio
    async def test_ensure_started_raises(self) -> None:
        handler = CloudflareHandler(_make_config())
        with pytest.raises(RuntimeError, match="start\\(\\) must be called"):
            await handler.execute(_make_event(), _make_action())


# ---------------------------------------------------------------------------
# can_handle
# ---------------------------------------------------------------------------


class TestCanHandle:
    @pytest.mark.asyncio
    async def test_known_operations(self) -> None:
        handler = _make_handler()
        for op in ("notify", "purge_cache", "purge_urls", "block_ip", "unblock_ip"):
            action = _make_action(operation=op)
            assert await handler.can_handle(_make_event(), action)

    @pytest.mark.asyncio
    async def test_unknown_operation(self) -> None:
        handler = _make_handler()
        action = _make_action(operation="delete_zone")
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
            params={"message": "Tunnel is down"},
        )

        result = await handler.execute(event, action)

        assert result.status == ActionStatus.SUCCESS
        assert result.details["message"] == "Tunnel is down"
        assert result.details["entity_id"] == "my-tunnel"
        assert result.duration_ms is not None

    @pytest.mark.asyncio
    async def test_notify_uses_description_as_fallback(self) -> None:
        handler = _make_handler()
        action = _make_action(
            operation="notify",
            description="WAF spike detected",
            params={},
        )

        result = await handler.execute(_make_event(), action)

        assert result.details["message"] == "WAF spike detected"


# ---------------------------------------------------------------------------
# purge_cache operation
# ---------------------------------------------------------------------------


class TestPurgeCacheOp:
    @pytest.mark.asyncio
    async def test_purge_cache_success(self) -> None:
        handler = _make_handler()
        handler._client.post = AsyncMock(
            return_value={"success": True, "result": {"id": "purge-1"}},
        )

        action = _make_action(operation="purge_cache")

        result = await handler.execute(_make_event(), action)

        assert result.status == ActionStatus.SUCCESS
        assert result.details["zone_id"] == "zone-abc"
        handler._client.post.assert_called_once_with(
            "/zones/zone-abc/purge_cache",
            {"purge_everything": True},
        )

    @pytest.mark.asyncio
    async def test_purge_cache_uses_param_zone_id(self) -> None:
        handler = _make_handler()
        handler._client.post = AsyncMock(
            return_value={"success": True, "result": {}},
        )

        action = _make_action(
            operation="purge_cache",
            params={"zone_id": "override-zone"},
        )

        result = await handler.execute(_make_event(), action)

        assert result.status == ActionStatus.SUCCESS
        assert result.details["zone_id"] == "override-zone"

    @pytest.mark.asyncio
    async def test_purge_cache_no_zone_fails(self) -> None:
        handler = _make_handler(zone_id="")
        action = _make_action(operation="purge_cache")

        result = await handler.execute(_make_event(), action)

        assert result.status == ActionStatus.FAILURE
        assert "zone_id" in result.error_message

    @pytest.mark.asyncio
    async def test_purge_cache_http_error(self) -> None:
        handler = _make_handler()
        handler._client.post = AsyncMock(
            side_effect=aiohttp.ClientError("connection refused"),
        )

        action = _make_action(operation="purge_cache")

        result = await handler.execute(_make_event(), action)

        assert result.status == ActionStatus.FAILURE
        assert "HTTP error" in result.error_message
        assert result.duration_ms is not None


# ---------------------------------------------------------------------------
# purge_urls operation
# ---------------------------------------------------------------------------


class TestPurgeUrlsOp:
    @pytest.mark.asyncio
    async def test_purge_urls_success(self) -> None:
        handler = _make_handler()
        handler._client.post = AsyncMock(
            return_value={"success": True, "result": {}},
        )

        action = _make_action(
            operation="purge_urls",
            params={"urls": ["https://example.com/page1", "https://example.com/page2"]},
        )

        result = await handler.execute(_make_event(), action)

        assert result.status == ActionStatus.SUCCESS
        assert result.details["url_count"] == 2
        handler._client.post.assert_called_once_with(
            "/zones/zone-abc/purge_cache",
            {"files": ["https://example.com/page1", "https://example.com/page2"]},
        )

    @pytest.mark.asyncio
    async def test_purge_urls_no_zone_fails(self) -> None:
        handler = _make_handler(zone_id="")
        action = _make_action(
            operation="purge_urls",
            params={"urls": ["https://example.com"]},
        )

        result = await handler.execute(_make_event(), action)

        assert result.status == ActionStatus.FAILURE
        assert "zone_id" in result.error_message

    @pytest.mark.asyncio
    async def test_purge_urls_no_urls_fails(self) -> None:
        handler = _make_handler()
        action = _make_action(operation="purge_urls", params={})

        result = await handler.execute(_make_event(), action)

        assert result.status == ActionStatus.FAILURE
        assert "urls" in result.error_message

    @pytest.mark.asyncio
    async def test_purge_urls_empty_list_fails(self) -> None:
        handler = _make_handler()
        action = _make_action(
            operation="purge_urls",
            params={"urls": []},
        )

        result = await handler.execute(_make_event(), action)

        assert result.status == ActionStatus.FAILURE
        assert "urls" in result.error_message


# ---------------------------------------------------------------------------
# block_ip operation
# ---------------------------------------------------------------------------


class TestBlockIpOp:
    @pytest.mark.asyncio
    async def test_block_ip_success(self) -> None:
        handler = _make_handler()
        handler._client.post = AsyncMock(return_value={
            "success": True,
            "result": {"id": "rule-123"},
        })

        action = _make_action(
            operation="block_ip",
            params={"ip": "192.168.1.100"},
        )

        result = await handler.execute(_make_event(), action)

        assert result.status == ActionStatus.SUCCESS
        assert result.details["ip"] == "192.168.1.100"
        assert result.details["rule_id"] == "rule-123"
        handler._client.post.assert_called_once_with(
            "/accounts/account-xyz/firewall/access_rules/rules",
            {
                "mode": "block",
                "configuration": {"target": "ip", "value": "192.168.1.100"},
                "notes": "Blocked by OasisAgent: Test action",
            },
        )

    @pytest.mark.asyncio
    async def test_block_ip_custom_note(self) -> None:
        handler = _make_handler()
        handler._client.post = AsyncMock(return_value={
            "success": True,
            "result": {"id": "rule-456"},
        })

        action = _make_action(
            operation="block_ip",
            params={"ip": "10.0.0.1", "note": "Suspicious traffic"},
        )

        result = await handler.execute(_make_event(), action)

        assert result.status == ActionStatus.SUCCESS
        call_json = handler._client.post.call_args[0][1]
        assert call_json["notes"] == "Suspicious traffic"

    @pytest.mark.asyncio
    async def test_block_ip_no_ip_fails(self) -> None:
        handler = _make_handler()
        action = _make_action(operation="block_ip", params={})

        result = await handler.execute(_make_event(), action)

        assert result.status == ActionStatus.FAILURE
        assert "ip" in result.error_message


# ---------------------------------------------------------------------------
# unblock_ip operation
# ---------------------------------------------------------------------------


class TestUnblockIpOp:
    @pytest.mark.asyncio
    async def test_unblock_ip_success(self) -> None:
        handler = _make_handler()
        handler._client.delete = AsyncMock(return_value={
            "success": True,
            "result": {"id": "rule-123"},
        })

        action = _make_action(
            operation="unblock_ip",
            params={"rule_id": "rule-123"},
        )

        result = await handler.execute(_make_event(), action)

        assert result.status == ActionStatus.SUCCESS
        assert result.details["rule_id"] == "rule-123"
        handler._client.delete.assert_called_once_with(
            "/accounts/account-xyz/firewall/access_rules/rules/rule-123",
        )

    @pytest.mark.asyncio
    async def test_unblock_ip_no_rule_id_fails(self) -> None:
        handler = _make_handler()
        action = _make_action(operation="unblock_ip", params={})

        result = await handler.execute(_make_event(), action)

        assert result.status == ActionStatus.FAILURE
        assert "rule_id" in result.error_message


# ---------------------------------------------------------------------------
# Unknown operation
# ---------------------------------------------------------------------------


class TestUnknownOp:
    @pytest.mark.asyncio
    async def test_unknown_operation_returns_failure(self) -> None:
        handler = _make_handler()
        action = _make_action(operation="delete_zone")

        result = await handler.execute(_make_event(), action)

        assert result.status == ActionStatus.FAILURE
        assert "Unknown operation" in result.error_message


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------


class TestVerify:
    @pytest.mark.asyncio
    async def test_verify_non_block_returns_true(self) -> None:
        handler = _make_handler()
        action = _make_action(operation="notify")
        result = ActionResult(status=ActionStatus.SUCCESS)

        verify = await handler.verify(_make_event(), action, result)

        assert verify.verified is True

    @pytest.mark.asyncio
    async def test_verify_purge_cache_returns_true(self) -> None:
        handler = _make_handler()
        action = _make_action(operation="purge_cache")
        result = ActionResult(status=ActionStatus.SUCCESS)

        verify = await handler.verify(_make_event(), action, result)

        assert verify.verified is True

    @pytest.mark.asyncio
    async def test_verify_block_ip_confirmed(self) -> None:
        handler = _make_handler()
        handler._client.get = AsyncMock(return_value={
            "success": True,
            "result": {"id": "rule-123", "mode": "block"},
        })

        action = _make_action(operation="block_ip")
        result = ActionResult(
            status=ActionStatus.SUCCESS,
            details={"rule_id": "rule-123", "ip": "1.2.3.4"},
        )

        verify = await handler.verify(_make_event(), action, result)

        assert verify.verified is True
        assert "rule-123" in verify.message

    @pytest.mark.asyncio
    async def test_verify_block_ip_not_found(self) -> None:
        handler = _make_handler()
        handler._client.get = AsyncMock(return_value={
            "success": True,
            "result": {"id": "different-rule"},
        })

        action = _make_action(operation="block_ip")
        result = ActionResult(
            status=ActionStatus.SUCCESS,
            details={"rule_id": "rule-123"},
        )

        verify = await handler.verify(_make_event(), action, result)

        assert verify.verified is False

    @pytest.mark.asyncio
    async def test_verify_block_ip_http_error(self) -> None:
        handler = _make_handler()
        handler._client.get = AsyncMock(
            side_effect=aiohttp.ClientError("timeout"),
        )

        action = _make_action(operation="block_ip")
        result = ActionResult(
            status=ActionStatus.SUCCESS,
            details={"rule_id": "rule-123"},
        )

        verify = await handler.verify(_make_event(), action, result)

        assert verify.verified is False
        assert "Failed to verify" in verify.message

    @pytest.mark.asyncio
    async def test_verify_block_ip_no_rule_id(self) -> None:
        handler = _make_handler()
        action = _make_action(operation="block_ip")
        result = ActionResult(
            status=ActionStatus.SUCCESS,
            details={},
        )

        verify = await handler.verify(_make_event(), action, result)

        assert verify.verified is False
        assert "No rule_id" in verify.message

    @pytest.mark.asyncio
    async def test_verify_block_ip_failed_result(self) -> None:
        """block_ip with FAILURE status skips verification."""
        handler = _make_handler()
        action = _make_action(operation="block_ip")
        result = ActionResult(status=ActionStatus.FAILURE)

        verify = await handler.verify(_make_event(), action, result)

        assert verify.verified is True
        assert "No verification needed" in verify.message


# ---------------------------------------------------------------------------
# get_context
# ---------------------------------------------------------------------------


class TestGetContext:
    @pytest.mark.asyncio
    async def test_get_context_zone_and_tunnels(self) -> None:
        handler = _make_handler()
        handler._client.get = AsyncMock(side_effect=[
            # /zones/{zone_id}
            {"result": {
                "name": "example.com",
                "status": "active",
                "paused": False,
                "plan": {"name": "Pro"},
            }},
            # /accounts/{account_id}/cfd_tunnel
            {"result": [
                {"id": "t1", "name": "main-tunnel", "status": "active"},
                {"id": "t2", "name": "backup-tunnel", "status": "inactive"},
            ]},
        ])

        context = await handler.get_context(_make_event())

        assert context["zone"]["name"] == "example.com"
        assert context["zone"]["plan"] == "Pro"
        assert len(context["tunnels"]) == 2
        assert context["tunnels"][0]["name"] == "main-tunnel"

    @pytest.mark.asyncio
    async def test_get_context_zone_error(self) -> None:
        handler = _make_handler()
        handler._client.get = AsyncMock(side_effect=[
            aiohttp.ClientError("connection refused"),
            {"result": [{"id": "t1", "name": "tunnel", "status": "active"}]},
        ])

        context = await handler.get_context(_make_event())

        assert "zone_error" in context
        assert len(context["tunnels"]) == 1

    @pytest.mark.asyncio
    async def test_get_context_tunnel_error(self) -> None:
        handler = _make_handler()
        handler._client.get = AsyncMock(side_effect=[
            {"result": {"name": "example.com", "status": "active",
                        "paused": False, "plan": {"name": "Free"}}},
            aiohttp.ClientError("timeout"),
        ])

        context = await handler.get_context(_make_event())

        assert context["zone"]["name"] == "example.com"
        assert "tunnel_error" in context

    @pytest.mark.asyncio
    async def test_get_context_no_zone_id(self) -> None:
        handler = _make_handler(zone_id="")
        handler._client.get = AsyncMock(return_value={
            "result": [{"id": "t1", "name": "tunnel", "status": "active"}],
        })

        context = await handler.get_context(_make_event())

        assert "zone" not in context
        assert "tunnels" in context

    @pytest.mark.asyncio
    async def test_get_context_no_account_id(self) -> None:
        handler = _make_handler(account_id="")
        handler._client.get = AsyncMock(return_value={
            "result": {"name": "example.com", "status": "active",
                        "paused": False, "plan": {"name": "Free"}},
        })

        context = await handler.get_context(_make_event())

        assert "zone" in context
        assert "tunnels" not in context


# ---------------------------------------------------------------------------
# Config & registry
# ---------------------------------------------------------------------------


class TestConfigAndRegistry:
    def test_config_defaults(self) -> None:
        config = CloudflareHandlerConfig()
        assert config.enabled is False
        assert config.timeout == 30

    def test_config_extra_forbidden(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="Extra inputs"):
            _make_config(bogus="nope")

    def test_handlers_config_has_cloudflare(self) -> None:
        from oasisagent.config import HandlersConfig

        config = HandlersConfig()
        assert hasattr(config, "cloudflare")
        assert isinstance(config.cloudflare, CloudflareHandlerConfig)

    def test_registry_entry(self) -> None:
        from oasisagent.db.registry import CORE_SERVICE_TYPES

        assert "cloudflare_handler" in CORE_SERVICE_TYPES
        meta = CORE_SERVICE_TYPES["cloudflare_handler"]
        assert meta.model is CloudflareHandlerConfig
        assert "api_token" in meta.secret_fields

    def test_handler_exported_from_init(self) -> None:
        from oasisagent.handlers import CloudflareHandler as Exported

        assert Exported is CloudflareHandler
