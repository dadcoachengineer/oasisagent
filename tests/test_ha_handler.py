"""Tests for the Home Assistant handler."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from oasisagent.config import HaHandlerConfig
from oasisagent.handlers.homeassistant import (
    HandlerNotStartedError,
    HomeAssistantHandler,
)
from oasisagent.models import (
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


def _make_config(**overrides: Any) -> HaHandlerConfig:
    defaults: dict[str, Any] = {
        "enabled": True,
        "url": "http://localhost:8123",
        "token": "test-token",
        "verify_timeout": 5,
        "verify_poll_interval": 0.1,
    }
    defaults.update(overrides)
    return HaHandlerConfig(**defaults)


def _make_event(**overrides: Any) -> Event:
    defaults: dict[str, Any] = {
        "source": "test",
        "system": "homeassistant",
        "event_type": "state_unavailable",
        "entity_id": "light.kitchen",
        "severity": Severity.WARNING,
        "timestamp": datetime.now(UTC),
        "payload": {},
        "metadata": EventMetadata(),
    }
    defaults.update(overrides)
    return Event(**defaults)


def _make_action(**overrides: Any) -> RecommendedAction:
    defaults: dict[str, Any] = {
        "description": "Test action",
        "handler": "homeassistant",
        "operation": "notify",
        "params": {},
        "risk_tier": RiskTier.RECOMMEND,
    }
    defaults.update(overrides)
    return RecommendedAction(**defaults)


def _mock_response(
    status: int = 200,
    json_data: dict[str, Any] | None = None,
    text_data: str = "",
) -> MagicMock:
    """Create a mock aiohttp response.

    Uses MagicMock (not AsyncMock) because aiohttp's raise_for_status()
    is synchronous. json()/text() are async.
    """
    resp = MagicMock()
    resp.status = status
    resp.json = AsyncMock(return_value=json_data or {})
    resp.text = AsyncMock(return_value=text_data)
    if status >= 400:
        resp.raise_for_status.side_effect = aiohttp.ClientResponseError(
            request_info=MagicMock(),
            history=(),
            status=status,
            message=f"HTTP {status}",
        )
    else:
        resp.raise_for_status.return_value = None
    return resp


def _patch_session(
    handler: HomeAssistantHandler, responses: dict[str, MagicMock]
) -> None:
    """Replace the handler's session with a mock that returns specified responses.

    responses maps method+path patterns to mock responses, e.g.:
        {"post:/api/services/automation/reload": _mock_response()}
    """
    session = MagicMock(spec=aiohttp.ClientSession)

    def _make_cm(resp: MagicMock) -> MagicMock:
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=resp)
        cm.__aexit__ = AsyncMock(return_value=False)
        return cm

    def _get_handler(path: str, **kwargs: Any) -> MagicMock:
        key = f"get:{path}"
        resp = responses.get(key, _mock_response())
        return _make_cm(resp)

    def _post_handler(path: str, **kwargs: Any) -> MagicMock:
        key = f"post:{path}"
        resp = responses.get(key, _mock_response())
        return _make_cm(resp)

    session.get = MagicMock(side_effect=_get_handler)
    session.post = MagicMock(side_effect=_post_handler)
    session.close = AsyncMock()

    handler._session = session


async def _started_handler(**config_overrides: Any) -> HomeAssistantHandler:
    """Create a handler with a mocked session (skipping real HTTP)."""
    handler = HomeAssistantHandler(_make_config(**config_overrides))
    handler._session = AsyncMock(spec=aiohttp.ClientSession)
    return handler


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    """Handler start/stop lifecycle."""

    async def test_start_creates_session(self) -> None:
        handler = HomeAssistantHandler(_make_config())
        with patch("oasisagent.handlers.homeassistant.aiohttp.ClientSession") as mock_cls:
            await handler.start()
            mock_cls.assert_called_once()
            assert handler._session is not None

    async def test_stop_closes_session(self) -> None:
        handler = HomeAssistantHandler(_make_config())
        mock_session = AsyncMock()
        handler._session = mock_session

        await handler.stop()

        mock_session.close.assert_called_once()
        assert handler._session is None

    async def test_stop_without_start_is_noop(self) -> None:
        handler = HomeAssistantHandler(_make_config())
        await handler.stop()  # Should not raise

    async def test_execute_before_start_raises(self) -> None:
        handler = HomeAssistantHandler(_make_config())

        with pytest.raises(HandlerNotStartedError):
            await handler.execute(_make_event(), _make_action())

    async def test_get_context_before_start_raises(self) -> None:
        handler = HomeAssistantHandler(_make_config())

        with pytest.raises(HandlerNotStartedError):
            await handler.get_context(_make_event())

    async def test_verify_before_start_raises_for_restart(self) -> None:
        handler = HomeAssistantHandler(_make_config())
        action = _make_action(operation="restart_integration")
        from oasisagent.models import ActionResult

        result = ActionResult(status=ActionStatus.SUCCESS)
        with pytest.raises(HandlerNotStartedError):
            await handler.verify(_make_event(), action, result)


# ---------------------------------------------------------------------------
# name()
# ---------------------------------------------------------------------------


class TestName:
    def test_name_is_homeassistant(self) -> None:
        handler = HomeAssistantHandler(_make_config())
        assert handler.name() == "homeassistant"


# ---------------------------------------------------------------------------
# can_handle()
# ---------------------------------------------------------------------------


class TestCanHandle:
    """can_handle whitelists known operations."""

    async def test_known_operation_accepted(self) -> None:
        handler = HomeAssistantHandler(_make_config())
        event = _make_event()

        for op in ["notify", "restart_integration", "reload_automations",
                    "call_service", "get_entity_state", "get_error_log"]:
            action = _make_action(operation=op)
            assert await handler.can_handle(event, action) is True

    async def test_unknown_operation_rejected(self) -> None:
        handler = HomeAssistantHandler(_make_config())
        action = _make_action(operation="delete_everything")

        assert await handler.can_handle(_make_event(), action) is False

    async def test_wrong_handler_rejected(self) -> None:
        handler = HomeAssistantHandler(_make_config())
        action = _make_action(handler="docker", operation="notify")

        assert await handler.can_handle(_make_event(), action) is False


# ---------------------------------------------------------------------------
# execute: notify
# ---------------------------------------------------------------------------


class TestNotify:
    async def test_notify_returns_success(self) -> None:
        handler = await _started_handler()
        _patch_session(handler, {})
        action = _make_action(
            operation="notify",
            params={"message": "Integration restarted"},
        )

        result = await handler.execute(_make_event(), action)

        assert result.status == ActionStatus.SUCCESS
        assert result.details["message"] == "Integration restarted"

    async def test_notify_uses_description_as_fallback(self) -> None:
        handler = await _started_handler()
        _patch_session(handler, {})
        action = _make_action(
            operation="notify",
            description="Fallback message",
            params={},
        )

        result = await handler.execute(_make_event(), action)

        assert result.details["message"] == "Fallback message"


# ---------------------------------------------------------------------------
# execute: restart_integration
# ---------------------------------------------------------------------------


class TestRestartIntegration:
    async def test_success(self) -> None:
        handler = await _started_handler()
        _patch_session(handler, {
            "post:/api/services/homeassistant/reload_config_entry": _mock_response(),
        })
        action = _make_action(
            operation="restart_integration",
            params={"entry_id": "abc123"},
        )

        result = await handler.execute(_make_event(), action)

        assert result.status == ActionStatus.SUCCESS
        assert result.details["entry_id"] == "abc123"
        assert result.duration_ms is not None

    async def test_missing_entry_id_fails(self) -> None:
        handler = await _started_handler()
        _patch_session(handler, {})
        action = _make_action(operation="restart_integration", params={})

        result = await handler.execute(_make_event(), action)

        assert result.status == ActionStatus.FAILURE
        assert "entry_id" in result.error_message

    async def test_http_error_returns_failure(self) -> None:
        handler = await _started_handler()
        _patch_session(handler, {
            "post:/api/services/homeassistant/reload_config_entry": _mock_response(status=500),
        })
        action = _make_action(
            operation="restart_integration",
            params={"entry_id": "abc123"},
        )

        result = await handler.execute(_make_event(), action)

        assert result.status == ActionStatus.FAILURE
        assert "HTTP error" in result.error_message


# ---------------------------------------------------------------------------
# execute: reload_automations
# ---------------------------------------------------------------------------


class TestReloadAutomations:
    async def test_success(self) -> None:
        handler = await _started_handler()
        _patch_session(handler, {
            "post:/api/services/automation/reload": _mock_response(),
        })
        action = _make_action(operation="reload_automations")

        result = await handler.execute(_make_event(), action)

        assert result.status == ActionStatus.SUCCESS


# ---------------------------------------------------------------------------
# execute: call_service
# ---------------------------------------------------------------------------


class TestCallService:
    async def test_success(self) -> None:
        handler = await _started_handler()
        _patch_session(handler, {
            "post:/api/services/light/turn_on": _mock_response(),
        })
        action = _make_action(
            operation="call_service",
            params={
                "domain": "light",
                "service": "turn_on",
                "service_data": {"entity_id": "light.kitchen"},
            },
        )

        result = await handler.execute(_make_event(), action)

        assert result.status == ActionStatus.SUCCESS
        assert result.details["domain"] == "light"
        assert result.details["service"] == "turn_on"

    async def test_missing_domain_fails(self) -> None:
        handler = await _started_handler()
        _patch_session(handler, {})
        action = _make_action(
            operation="call_service",
            params={"service": "turn_on"},
        )

        result = await handler.execute(_make_event(), action)

        assert result.status == ActionStatus.FAILURE
        assert "domain" in result.error_message

    async def test_missing_service_fails(self) -> None:
        handler = await _started_handler()
        _patch_session(handler, {})
        action = _make_action(
            operation="call_service",
            params={"domain": "light"},
        )

        result = await handler.execute(_make_event(), action)

        assert result.status == ActionStatus.FAILURE
        assert "service" in result.error_message


# ---------------------------------------------------------------------------
# execute: get_entity_state
# ---------------------------------------------------------------------------


class TestGetEntityState:
    async def test_success(self) -> None:
        state_data = {"entity_id": "light.kitchen", "state": "on"}
        handler = await _started_handler()
        _patch_session(handler, {
            "get:/api/states/light.kitchen": _mock_response(json_data=state_data),
        })
        action = _make_action(
            operation="get_entity_state",
            params={"entity_id": "light.kitchen"},
        )

        result = await handler.execute(_make_event(), action)

        assert result.status == ActionStatus.SUCCESS
        assert result.details["state"] == state_data

    async def test_uses_event_entity_id_as_fallback(self) -> None:
        handler = await _started_handler()
        _patch_session(handler, {
            "get:/api/states/light.kitchen": _mock_response(json_data={"state": "on"}),
        })
        action = _make_action(operation="get_entity_state", params={})

        result = await handler.execute(
            _make_event(entity_id="light.kitchen"), action
        )

        assert result.status == ActionStatus.SUCCESS


# ---------------------------------------------------------------------------
# execute: get_error_log
# ---------------------------------------------------------------------------


class TestGetErrorLog:
    async def test_success(self) -> None:
        handler = await _started_handler()
        _patch_session(handler, {
            "get:/api/error_log": _mock_response(text_data="Error: something broke\n"),
        })
        action = _make_action(operation="get_error_log")

        result = await handler.execute(_make_event(), action)

        assert result.status == ActionStatus.SUCCESS
        assert "something broke" in result.details["error_log"]


# ---------------------------------------------------------------------------
# execute: unknown operation
# ---------------------------------------------------------------------------


class TestUnknownOperation:
    async def test_unknown_operation_returns_failure(self) -> None:
        handler = await _started_handler()
        _patch_session(handler, {})
        action = _make_action(operation="delete_everything")

        result = await handler.execute(_make_event(), action)

        assert result.status == ActionStatus.FAILURE
        assert "Unknown operation" in result.error_message


# ---------------------------------------------------------------------------
# execute: duration tracking
# ---------------------------------------------------------------------------


class TestDurationTracking:
    async def test_duration_ms_set_on_success(self) -> None:
        handler = await _started_handler()
        _patch_session(handler, {})
        action = _make_action(operation="notify")

        result = await handler.execute(_make_event(), action)

        assert result.duration_ms is not None
        assert result.duration_ms >= 0

    async def test_duration_ms_set_on_http_error(self) -> None:
        handler = await _started_handler()
        _patch_session(handler, {
            "post:/api/services/homeassistant/reload_config_entry": _mock_response(status=500),
        })
        action = _make_action(
            operation="restart_integration",
            params={"entry_id": "abc123"},
        )

        result = await handler.execute(_make_event(), action)

        assert result.duration_ms is not None


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------


class TestVerify:
    """Verification after action execution."""

    async def test_non_restart_returns_verified(self) -> None:
        handler = HomeAssistantHandler(_make_config())
        action = _make_action(operation="notify")
        from oasisagent.models import ActionResult

        result = ActionResult(status=ActionStatus.SUCCESS)
        verify = await handler.verify(_make_event(), action, result)

        assert verify.verified is True

    async def test_restart_verified_when_entity_recovers(self) -> None:
        handler = await _started_handler(verify_timeout=5, verify_poll_interval=0.05)
        _patch_session(handler, {
            "get:/api/states/light.kitchen": _mock_response(
                json_data={"state": "on"}
            ),
        })
        action = _make_action(operation="restart_integration")
        from oasisagent.models import ActionResult

        result = ActionResult(status=ActionStatus.SUCCESS)
        verify = await handler.verify(
            _make_event(entity_id="light.kitchen"), action, result
        )

        assert verify.verified is True
        assert "recovered" in verify.message

    async def test_restart_not_verified_when_still_unavailable(self) -> None:
        handler = await _started_handler(verify_timeout=1, verify_poll_interval=0.05)
        _patch_session(handler, {
            "get:/api/states/light.kitchen": _mock_response(
                json_data={"state": "unavailable"}
            ),
        })
        action = _make_action(operation="restart_integration")
        from oasisagent.models import ActionResult

        result = ActionResult(status=ActionStatus.SUCCESS)
        verify = await handler.verify(
            _make_event(entity_id="light.kitchen"), action, result
        )

        assert verify.verified is False
        assert "did not recover" in verify.message


# ---------------------------------------------------------------------------
# get_context
# ---------------------------------------------------------------------------


class TestGetContext:
    async def test_gathers_state_and_log(self) -> None:
        handler = await _started_handler()
        _patch_session(handler, {
            "get:/api/states/light.kitchen": _mock_response(
                json_data={"state": "unavailable"}
            ),
            "get:/api/error_log": _mock_response(text_data="Error log text"),
        })

        context = await handler.get_context(
            _make_event(entity_id="light.kitchen")
        )

        assert "entity_state" in context
        assert "recent_errors" in context

    async def test_handles_state_error_gracefully(self) -> None:
        handler = await _started_handler()
        _patch_session(handler, {
            "get:/api/states/light.kitchen": _mock_response(status=404),
            "get:/api/error_log": _mock_response(text_data="logs"),
        })

        context = await handler.get_context(
            _make_event(entity_id="light.kitchen")
        )

        assert "entity_state_error" in context
        assert "recent_errors" in context
