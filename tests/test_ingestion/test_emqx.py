"""Tests for the EMQX MQTT broker ingestion adapter."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from pydantic import ValidationError

from oasisagent.config import EmqxAdapterConfig
from oasisagent.ingestion.emqx import EmqxAdapter
from oasisagent.models import Severity


def _make_config(**overrides: object) -> EmqxAdapterConfig:
    defaults: dict[str, object] = {
        "enabled": True,
        "url": "http://emqx.local:18083",
        "api_key": "test-key",
        "api_secret": "test-secret",
        "poll_interval": 60,
        "timeout": 10,
    }
    defaults.update(overrides)
    return EmqxAdapterConfig(**defaults)  # type: ignore[arg-type]


def _mock_queue() -> MagicMock:
    return MagicMock()


def _make_adapter(**overrides: object) -> tuple[EmqxAdapter, MagicMock]:
    config = _make_config(**overrides)
    queue = _mock_queue()
    adapter = EmqxAdapter(config, queue)
    return adapter, queue


def _mock_response(status: int = 200, json_data: object = None) -> AsyncMock:
    mock = AsyncMock()
    mock.status = status
    mock.raise_for_status = MagicMock()
    mock.json = AsyncMock(return_value=json_data if json_data is not None else {})
    mock.__aenter__ = AsyncMock(return_value=mock)
    mock.__aexit__ = AsyncMock(return_value=False)
    return mock


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


class TestConfigValidation:
    def test_defaults(self) -> None:
        config = EmqxAdapterConfig()
        assert config.enabled is False
        assert config.api_key == ""
        assert config.api_secret == ""
        assert config.poll_interval == 60

    def test_extra_fields_forbidden(self) -> None:
        with pytest.raises(ValidationError, match="Extra inputs"):
            _make_config(bogus="nope")


# ---------------------------------------------------------------------------
# Adapter identity & lifecycle
# ---------------------------------------------------------------------------


class TestAdapterLifecycle:
    def test_name(self) -> None:
        adapter, _ = _make_adapter()
        assert adapter.name == "emqx"

    @pytest.mark.asyncio
    async def test_healthy_initially_false(self) -> None:
        adapter, _ = _make_adapter()
        assert not await adapter.healthy()

    @pytest.mark.asyncio
    async def test_stop_sets_flag(self) -> None:
        adapter, _ = _make_adapter()
        await adapter.stop()
        assert adapter._stopping is True


# ---------------------------------------------------------------------------
# Health polling
# ---------------------------------------------------------------------------


class TestHealthPolling:
    @pytest.mark.asyncio
    async def test_first_poll_ok_no_event(self) -> None:
        adapter, queue = _make_adapter()

        # stats, alarms, listeners
        responses = [
            _mock_response(200),
            _mock_response(200, []),
            _mock_response(200, []),
        ]
        mock_session = AsyncMock()
        mock_session.get = MagicMock(side_effect=responses)

        await adapter._poll_health(mock_session)
        queue.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_recovery_emits_event(self) -> None:
        adapter, queue = _make_adapter()
        adapter._service_ok = False

        responses = [
            _mock_response(200),
            _mock_response(200, []),
            _mock_response(200, []),
        ]
        mock_session = AsyncMock()
        mock_session.get = MagicMock(side_effect=responses)

        await adapter._poll_health(mock_session)

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "emqx_recovered"
        assert event.severity == Severity.INFO

    @pytest.mark.asyncio
    async def test_uses_basic_auth(self) -> None:
        adapter, _ = _make_adapter()

        responses = [
            _mock_response(200),
            _mock_response(200, []),
            _mock_response(200, []),
        ]
        mock_session = AsyncMock()
        mock_session.get = MagicMock(side_effect=responses)

        await adapter._poll_health(mock_session)

        # All three calls should use basic auth
        for call in mock_session.get.call_args_list:
            assert call[1]["auth"] is not None


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------


class TestFailureHandling:
    def test_first_failure_emits_unreachable(self) -> None:
        adapter, queue = _make_adapter()

        adapter._handle_failure("connection refused")

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "emqx_unreachable"
        assert event.severity == Severity.ERROR

    def test_already_down_no_duplicate(self) -> None:
        adapter, queue = _make_adapter()
        adapter._service_ok = False

        adapter._handle_failure("still down")
        queue.put_nowait.assert_not_called()

    def test_failure_resets_state(self) -> None:
        adapter, _queue = _make_adapter()
        adapter._service_ok = True
        adapter._active_alarms = {"high_cpu"}
        adapter._down_listeners = {"tcp:1883"}

        adapter._handle_failure("lost")

        assert adapter._active_alarms == set()
        assert adapter._down_listeners == set()


# ---------------------------------------------------------------------------
# Alarm monitoring
# ---------------------------------------------------------------------------


class TestAlarmMonitoring:
    @pytest.mark.asyncio
    async def test_new_alarm_emits_event(self) -> None:
        adapter, queue = _make_adapter()

        mock_session = AsyncMock()
        mock_session.get = MagicMock(
            return_value=_mock_response(
                200, [{"name": "high_cpu_usage", "activated": True}],
            ),
        )

        await adapter._poll_alarms(mock_session)

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "emqx_alarm_active"
        assert event.payload["alarm_name"] == "high_cpu_usage"

    @pytest.mark.asyncio
    async def test_known_alarm_no_duplicate(self) -> None:
        adapter, queue = _make_adapter()
        adapter._active_alarms = {"high_cpu_usage"}

        mock_session = AsyncMock()
        mock_session.get = MagicMock(
            return_value=_mock_response(
                200, [{"name": "high_cpu_usage", "activated": True}],
            ),
        )

        await adapter._poll_alarms(mock_session)
        queue.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_cleared_alarm_removed(self) -> None:
        adapter, _queue = _make_adapter()
        adapter._active_alarms = {"old_alarm"}

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=_mock_response(200, []))

        await adapter._poll_alarms(mock_session)
        assert adapter._active_alarms == set()


# ---------------------------------------------------------------------------
# Listener monitoring
# ---------------------------------------------------------------------------


class TestListenerMonitoring:
    @pytest.mark.asyncio
    async def test_down_listener_emits_event(self) -> None:
        adapter, queue = _make_adapter()

        mock_session = AsyncMock()
        mock_session.get = MagicMock(
            return_value=_mock_response(
                200, [{"id": "tcp:1883", "running": False}],
            ),
        )

        await adapter._poll_listeners(mock_session)

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "emqx_listener_down"
        assert event.payload["listener_id"] == "tcp:1883"

    @pytest.mark.asyncio
    async def test_running_listener_no_event(self) -> None:
        adapter, queue = _make_adapter()

        mock_session = AsyncMock()
        mock_session.get = MagicMock(
            return_value=_mock_response(
                200, [{"id": "tcp:1883", "running": True}],
            ),
        )

        await adapter._poll_listeners(mock_session)
        queue.put_nowait.assert_not_called()


# ---------------------------------------------------------------------------
# Poll loop backoff
# ---------------------------------------------------------------------------


class TestPollLoopBackoff:
    @pytest.mark.asyncio
    async def test_backoff_increases(self) -> None:
        adapter, _ = _make_adapter(poll_interval=10)

        poll_count = 0
        sleep_totals: list[int] = []
        current_total = 0

        original_sleep = asyncio.sleep

        async def _tracking_sleep(_: float) -> None:
            nonlocal current_total
            current_total += 1
            await original_sleep(0)

        async def _fail_poll(_session: object) -> None:
            nonlocal poll_count, current_total
            if poll_count > 0:
                sleep_totals.append(current_total)
                current_total = 0
            poll_count += 1
            if poll_count >= 4:
                adapter._stopping = True
                return
            raise aiohttp.ClientError("refused")

        adapter._poll_health = _fail_poll  # type: ignore[method-assign]

        with patch(
            "oasisagent.ingestion.emqx.aiohttp.ClientSession",
        ) as mock_cs, patch(
            "oasisagent.ingestion.emqx.asyncio.sleep",
            side_effect=_tracking_sleep,
        ):
            mock_session = AsyncMock()
            mock_cs.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_cs.return_value.__aexit__ = AsyncMock(return_value=False)
            await adapter._poll_loop()

        assert len(sleep_totals) >= 2
        for i in range(1, len(sleep_totals)):
            assert sleep_totals[i] >= sleep_totals[i - 1]


# ---------------------------------------------------------------------------
# Registry & form specs
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_emqx_connector_registered(self) -> None:
        from oasisagent.db.registry import CONNECTOR_TYPES

        assert "emqx" in CONNECTOR_TYPES
        meta = CONNECTOR_TYPES["emqx"]
        assert meta.model is EmqxAdapterConfig
        assert meta.secret_fields == frozenset({"api_key", "api_secret"})

    def test_form_specs_exist(self) -> None:
        from oasisagent.ui.form_specs import FORM_SPECS

        assert "emqx" in FORM_SPECS
        field_names = {s.name for s in FORM_SPECS["emqx"]}
        assert "url" in field_names
        assert "api_key" in field_names
        assert "api_secret" in field_names

    def test_display_name(self) -> None:
        from oasisagent.ui.form_specs import TYPE_DISPLAY_NAMES

        assert TYPE_DISPLAY_NAMES["emqx"] == "EMQX"


# ---------------------------------------------------------------------------
# Known fixes
# ---------------------------------------------------------------------------


class TestKnownFixes:
    def test_known_fixes_exist(self) -> None:
        from pathlib import Path

        import yaml

        fixes_path = Path(__file__).parent.parent.parent / "known_fixes" / "emqx.yaml"
        assert fixes_path.exists()

        with fixes_path.open() as f:
            data = yaml.safe_load(f)

        fix_ids = {fix["id"] for fix in data["fixes"]}
        assert "emqx-unreachable" in fix_ids
        assert "emqx-alarm-active" in fix_ids
        assert "emqx-listener-down" in fix_ids

    def test_emqx_fixes_are_recommend_or_escalate(self) -> None:
        """EMQX is meta-monitoring — never auto_fix."""
        from pathlib import Path

        import yaml

        fixes_path = Path(__file__).parent.parent.parent / "known_fixes" / "emqx.yaml"
        with fixes_path.open() as f:
            data = yaml.safe_load(f)

        for fix in data["fixes"]:
            assert fix["risk_tier"] in ("recommend", "escalate"), (
                f"EMQX fix {fix['id']} must not be auto_fix (circularity)"
            )
