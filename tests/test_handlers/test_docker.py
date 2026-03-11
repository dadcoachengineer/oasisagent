"""Tests for the Docker handler."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from oasisagent.config import DockerHandlerConfig
from oasisagent.handlers.docker import (
    DockerHandler,
    HandlerNotStartedError,
)
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


def _make_config(**overrides: Any) -> DockerHandlerConfig:
    defaults: dict[str, Any] = {
        "enabled": True,
        "socket": "unix:///var/run/docker.sock",
        "verify_timeout": 5,
        "verify_poll_interval": 0.05,
    }
    defaults.update(overrides)
    return DockerHandlerConfig(**defaults)


def _make_event(**overrides: Any) -> Event:
    defaults: dict[str, Any] = {
        "source": "test",
        "system": "docker",
        "event_type": "container_die",
        "entity_id": "my_container",
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
        "handler": "docker",
        "operation": "restart_container",
        "params": {"container_id": "my_container"},
        "risk_tier": RiskTier.AUTO_FIX,
    }
    defaults.update(overrides)
    return RecommendedAction(**defaults)


def _mock_response(
    status: int = 200,
    json_data: dict[str, Any] | None = None,
    text_data: str = "",
) -> MagicMock:
    """Create a mock aiohttp response."""
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
    handler: DockerHandler, responses: dict[str, MagicMock]
) -> None:
    """Replace the handler's session with a mock returning specified responses."""
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


async def _started_handler(**config_overrides: Any) -> DockerHandler:
    """Create a handler with a mocked session (skipping real HTTP)."""
    handler = DockerHandler(_make_config(**config_overrides))
    handler._session = AsyncMock(spec=aiohttp.ClientSession)
    return handler


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    async def test_start_creates_session_unix_socket(self) -> None:
        handler = DockerHandler(_make_config())
        with (
            patch("oasisagent.handlers.docker.aiohttp.UnixConnector") as mock_conn,
            patch("oasisagent.handlers.docker.aiohttp.ClientSession") as mock_cls,
        ):
            await handler.start()
            mock_conn.assert_called_once_with(path="/var/run/docker.sock")
            mock_cls.assert_called_once()
            assert handler._session is not None

    async def test_start_creates_session_tcp(self) -> None:
        handler = DockerHandler(_make_config(url="http://docker-host:2375"))
        with (
            patch("oasisagent.handlers.docker.aiohttp.TCPConnector") as mock_conn,
            patch("oasisagent.handlers.docker.aiohttp.ClientSession") as mock_cls,
        ):
            await handler.start()
            mock_conn.assert_called_once()
            mock_cls.assert_called_once()

    async def test_start_tcp_https_respects_tls_verify(self) -> None:
        config = _make_config(url="https://docker-host:2376", tls_verify=False)
        handler = DockerHandler(config)
        with (
            patch("oasisagent.handlers.docker.aiohttp.TCPConnector") as mock_conn,
            patch("oasisagent.handlers.docker.aiohttp.ClientSession"),
        ):
            await handler.start()
            mock_conn.assert_called_once_with(ssl=False)

    async def test_stop_closes_session(self) -> None:
        handler = DockerHandler(_make_config())
        mock_session = AsyncMock()
        handler._session = mock_session

        await handler.stop()

        mock_session.close.assert_called_once()
        assert handler._session is None

    async def test_stop_without_start_is_noop(self) -> None:
        handler = DockerHandler(_make_config())
        await handler.stop()  # Should not raise

    async def test_execute_before_start_raises(self) -> None:
        handler = DockerHandler(_make_config())

        with pytest.raises(HandlerNotStartedError):
            await handler.execute(_make_event(), _make_action())

    async def test_get_context_before_start_raises(self) -> None:
        handler = DockerHandler(_make_config())

        with pytest.raises(HandlerNotStartedError):
            await handler.get_context(_make_event())

    async def test_verify_before_start_raises_for_restart(self) -> None:
        handler = DockerHandler(_make_config())
        action = _make_action(operation="restart_container")
        result = ActionResult(
            status=ActionStatus.SUCCESS,
            details={"container_id": "my_container"},
        )

        with pytest.raises(HandlerNotStartedError):
            await handler.verify(_make_event(), action, result)


# ---------------------------------------------------------------------------
# name()
# ---------------------------------------------------------------------------


class TestName:
    def test_name_is_docker(self) -> None:
        handler = DockerHandler(_make_config())
        assert handler.name() == "docker"


# ---------------------------------------------------------------------------
# can_handle()
# ---------------------------------------------------------------------------


class TestCanHandle:
    async def test_known_operations_accepted(self) -> None:
        handler = DockerHandler(_make_config())
        event = _make_event()

        for op in ["restart_container", "get_container_logs",
                    "get_container_stats", "inspect_container"]:
            action = _make_action(operation=op)
            assert await handler.can_handle(event, action) is True

    async def test_unknown_operation_rejected(self) -> None:
        handler = DockerHandler(_make_config())
        action = _make_action(operation="delete_container")

        assert await handler.can_handle(_make_event(), action) is False

    async def test_wrong_handler_rejected(self) -> None:
        handler = DockerHandler(_make_config())
        action = _make_action(handler="homeassistant", operation="restart_container")

        assert await handler.can_handle(_make_event(), action) is False


# ---------------------------------------------------------------------------
# execute: restart_container
# ---------------------------------------------------------------------------


class TestRestartContainer:
    async def test_success(self) -> None:
        handler = await _started_handler()
        _patch_session(handler, {
            "post:/containers/my_container/restart": _mock_response(status=204),
        })
        action = _make_action(
            operation="restart_container",
            params={"container_id": "my_container"},
        )

        result = await handler.execute(_make_event(), action)

        assert result.status == ActionStatus.SUCCESS
        assert result.details["container_id"] == "my_container"

    async def test_uses_event_entity_id_as_fallback(self) -> None:
        handler = await _started_handler()
        _patch_session(handler, {
            "post:/containers/nginx/restart": _mock_response(status=204),
        })
        action = _make_action(
            operation="restart_container",
            params={},  # no container_id
        )

        result = await handler.execute(_make_event(entity_id="nginx"), action)

        assert result.status == ActionStatus.SUCCESS
        assert result.details["container_id"] == "nginx"

    async def test_missing_container_id_fails(self) -> None:
        handler = await _started_handler()
        _patch_session(handler, {})
        action = _make_action(
            operation="restart_container",
            params={},
        )

        result = await handler.execute(_make_event(entity_id=""), action)

        assert result.status == ActionStatus.FAILURE
        assert "container_id" in (result.error_message or "")

    async def test_http_error_returns_failure(self) -> None:
        handler = await _started_handler()
        _patch_session(handler, {
            "post:/containers/my_container/restart": _mock_response(status=500),
        })
        action = _make_action(
            operation="restart_container",
            params={"container_id": "my_container"},
        )

        result = await handler.execute(_make_event(), action)

        assert result.status == ActionStatus.FAILURE
        assert "HTTP error" in (result.error_message or "")

    async def test_timeout_param_passed(self) -> None:
        handler = await _started_handler()
        _patch_session(handler, {
            "post:/containers/my_container/restart": _mock_response(status=204),
        })
        action = _make_action(
            operation="restart_container",
            params={"container_id": "my_container", "timeout": 10},
        )

        result = await handler.execute(_make_event(), action)

        assert result.status == ActionStatus.SUCCESS


# ---------------------------------------------------------------------------
# execute: get_container_logs
# ---------------------------------------------------------------------------


class TestGetContainerLogs:
    async def test_success(self) -> None:
        handler = await _started_handler()
        _patch_session(handler, {
            "get:/containers/my_container/logs": _mock_response(
                text_data="2024-01-01 Error: something failed\n"
            ),
        })
        action = _make_action(
            operation="get_container_logs",
            params={"container_id": "my_container"},
        )

        result = await handler.execute(_make_event(), action)

        assert result.status == ActionStatus.SUCCESS
        assert "something failed" in result.details["logs"]

    async def test_http_error_returns_failure(self) -> None:
        handler = await _started_handler()
        _patch_session(handler, {
            "get:/containers/my_container/logs": _mock_response(status=404),
        })
        action = _make_action(
            operation="get_container_logs",
            params={"container_id": "my_container"},
        )

        result = await handler.execute(_make_event(), action)

        assert result.status == ActionStatus.FAILURE


# ---------------------------------------------------------------------------
# execute: get_container_stats
# ---------------------------------------------------------------------------


class TestGetContainerStats:
    async def test_success(self) -> None:
        stats_data = {"cpu_stats": {"total_usage": 100}, "memory_stats": {"usage": 1024}}
        handler = await _started_handler()
        _patch_session(handler, {
            "get:/containers/my_container/stats": _mock_response(json_data=stats_data),
        })
        action = _make_action(
            operation="get_container_stats",
            params={"container_id": "my_container"},
        )

        result = await handler.execute(_make_event(), action)

        assert result.status == ActionStatus.SUCCESS
        assert result.details["stats"] == stats_data

    async def test_http_error_returns_failure(self) -> None:
        handler = await _started_handler()
        _patch_session(handler, {
            "get:/containers/my_container/stats": _mock_response(status=500),
        })
        action = _make_action(
            operation="get_container_stats",
            params={"container_id": "my_container"},
        )

        result = await handler.execute(_make_event(), action)

        assert result.status == ActionStatus.FAILURE


# ---------------------------------------------------------------------------
# execute: inspect_container
# ---------------------------------------------------------------------------


class TestInspectContainer:
    async def test_success(self) -> None:
        inspect_data = {
            "Id": "abc123",
            "State": {"Status": "running", "OOMKilled": False},
            "Config": {"Image": "nginx:latest"},
        }
        handler = await _started_handler()
        _patch_session(handler, {
            "get:/containers/my_container/json": _mock_response(json_data=inspect_data),
        })
        action = _make_action(
            operation="inspect_container",
            params={"container_id": "my_container"},
        )

        result = await handler.execute(_make_event(), action)

        assert result.status == ActionStatus.SUCCESS
        assert result.details["inspect"]["Id"] == "abc123"

    async def test_http_error_returns_failure(self) -> None:
        handler = await _started_handler()
        _patch_session(handler, {
            "get:/containers/my_container/json": _mock_response(status=404),
        })
        action = _make_action(
            operation="inspect_container",
            params={"container_id": "my_container"},
        )

        result = await handler.execute(_make_event(), action)

        assert result.status == ActionStatus.FAILURE


# ---------------------------------------------------------------------------
# execute: unknown operation
# ---------------------------------------------------------------------------


class TestUnknownOperation:
    async def test_unknown_operation_returns_failure(self) -> None:
        handler = await _started_handler()
        _patch_session(handler, {})
        action = _make_action(operation="delete_container")

        result = await handler.execute(_make_event(), action)

        assert result.status == ActionStatus.FAILURE
        assert "Unknown operation" in (result.error_message or "")


# ---------------------------------------------------------------------------
# execute: duration tracking
# ---------------------------------------------------------------------------


class TestDurationTracking:
    async def test_duration_ms_set_on_success(self) -> None:
        handler = await _started_handler()
        _patch_session(handler, {
            "post:/containers/my_container/restart": _mock_response(status=204),
        })
        action = _make_action(
            operation="restart_container",
            params={"container_id": "my_container"},
        )

        result = await handler.execute(_make_event(), action)

        assert result.duration_ms is not None
        assert result.duration_ms >= 0

    async def test_duration_ms_set_on_http_error(self) -> None:
        handler = await _started_handler()
        _patch_session(handler, {
            "post:/containers/my_container/restart": _mock_response(status=500),
        })
        action = _make_action(
            operation="restart_container",
            params={"container_id": "my_container"},
        )

        result = await handler.execute(_make_event(), action)

        assert result.duration_ms is not None


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------


class TestVerify:
    async def test_non_restart_returns_verified(self) -> None:
        handler = DockerHandler(_make_config())
        action = _make_action(operation="inspect_container")
        result = ActionResult(status=ActionStatus.SUCCESS)

        verify = await handler.verify(_make_event(), action, result)

        assert verify.verified is True

    async def test_restart_verified_when_container_running(self) -> None:
        handler = await _started_handler(verify_timeout=5, verify_poll_interval=0.05)
        _patch_session(handler, {
            "get:/containers/my_container/json": _mock_response(
                json_data={"State": {"Status": "running"}}
            ),
        })
        action = _make_action(operation="restart_container")
        result = ActionResult(
            status=ActionStatus.SUCCESS,
            details={"container_id": "my_container"},
        )

        verify = await handler.verify(_make_event(), action, result)

        assert verify.verified is True
        assert "running" in verify.message

    async def test_restart_not_verified_when_still_exited(self) -> None:
        handler = await _started_handler(verify_timeout=1, verify_poll_interval=0.05)
        _patch_session(handler, {
            "get:/containers/my_container/json": _mock_response(
                json_data={"State": {"Status": "exited"}}
            ),
        })
        action = _make_action(operation="restart_container")
        result = ActionResult(
            status=ActionStatus.SUCCESS,
            details={"container_id": "my_container"},
        )

        verify = await handler.verify(_make_event(), action, result)

        assert verify.verified is False
        assert "did not reach" in verify.message


# ---------------------------------------------------------------------------
# get_context
# ---------------------------------------------------------------------------


class TestGetContext:
    async def test_gathers_inspect_and_logs(self) -> None:
        handler = await _started_handler()
        inspect_data = {"State": {"Status": "exited", "OOMKilled": True}}
        _patch_session(handler, {
            "get:/containers/my_container/json": _mock_response(json_data=inspect_data),
            "get:/containers/my_container/logs": _mock_response(
                text_data="Error: out of memory\n"
            ),
        })

        context = await handler.get_context(_make_event(entity_id="my_container"))

        assert "container_inspect" in context
        assert context["container_inspect"]["State"]["OOMKilled"] is True
        assert "container_logs" in context
        assert "out of memory" in context["container_logs"]

    async def test_handles_inspect_error_gracefully(self) -> None:
        handler = await _started_handler()
        _patch_session(handler, {
            "get:/containers/my_container/json": _mock_response(status=404),
            "get:/containers/my_container/logs": _mock_response(text_data="logs"),
        })

        context = await handler.get_context(_make_event(entity_id="my_container"))

        assert "container_inspect_error" in context
        assert "container_logs" in context

    async def test_handles_logs_error_gracefully(self) -> None:
        handler = await _started_handler()
        inspect_data = {"State": {"Status": "running"}}
        _patch_session(handler, {
            "get:/containers/my_container/json": _mock_response(json_data=inspect_data),
            "get:/containers/my_container/logs": _mock_response(status=500),
        })

        context = await handler.get_context(_make_event(entity_id="my_container"))

        assert "container_inspect" in context
        assert "container_logs_error" in context
