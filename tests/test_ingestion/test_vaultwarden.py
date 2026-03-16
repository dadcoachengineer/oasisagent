"""Tests for the Vaultwarden health-check ingestion adapter."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from pydantic import ValidationError

from oasisagent.config import VaultwardenAdapterConfig
from oasisagent.ingestion.vaultwarden import VaultwardenAdapter
from oasisagent.models import Severity


def _make_config(**overrides: object) -> VaultwardenAdapterConfig:
    defaults: dict[str, object] = {
        "enabled": True,
        "url": "http://localhost:8000",
        "poll_interval": 60,
        "timeout": 10,
    }
    defaults.update(overrides)
    return VaultwardenAdapterConfig(**defaults)  # type: ignore[arg-type]


def _mock_queue() -> MagicMock:
    return MagicMock()


def _make_adapter(**overrides: object) -> tuple[VaultwardenAdapter, MagicMock]:
    config = _make_config(**overrides)
    queue = _mock_queue()
    adapter = VaultwardenAdapter(config, queue)
    return adapter, queue


def _mock_response(status: int = 200) -> AsyncMock:
    mock = AsyncMock()
    mock.status = status
    mock.raise_for_status = MagicMock()
    mock.__aenter__ = AsyncMock(return_value=mock)
    mock.__aexit__ = AsyncMock(return_value=False)
    return mock


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


class TestConfigValidation:
    def test_defaults(self) -> None:
        config = VaultwardenAdapterConfig()
        assert config.enabled is False
        assert config.poll_interval == 60
        assert config.timeout == 10

    def test_deep_health_defaults(self) -> None:
        config = VaultwardenAdapterConfig()
        assert config.deep_health is False
        assert config.admin_token == ""
        assert config.slow_threshold_ms == 2000

    def test_deep_health_custom(self) -> None:
        config = _make_config(
            deep_health=True, admin_token="secret", slow_threshold_ms=1000,
        )
        assert config.deep_health is True
        assert config.admin_token == "secret"
        assert config.slow_threshold_ms == 1000

    def test_extra_fields_forbidden(self) -> None:
        with pytest.raises(ValidationError, match="Extra inputs"):
            _make_config(bogus="nope")


# ---------------------------------------------------------------------------
# Adapter identity & lifecycle
# ---------------------------------------------------------------------------


class TestAdapterLifecycle:
    def test_name(self) -> None:
        adapter, _ = _make_adapter()
        assert adapter.name == "vaultwarden"

    @pytest.mark.asyncio
    async def test_healthy_initially_false(self) -> None:
        adapter, _ = _make_adapter()
        assert not await adapter.healthy()

    @pytest.mark.asyncio
    async def test_stop_sets_flag(self) -> None:
        adapter, _ = _make_adapter()
        await adapter.stop()
        assert adapter._stopping is True

    def test_deep_health_state_initial(self) -> None:
        adapter, _ = _make_adapter(deep_health=True)
        assert adapter._api_ok is None
        assert adapter._is_slow is None


# ---------------------------------------------------------------------------
# Health polling — state transitions
# ---------------------------------------------------------------------------


class TestHealthPolling:
    @pytest.mark.asyncio
    async def test_first_poll_ok_no_event(self) -> None:
        adapter, queue = _make_adapter()

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=_mock_response(200))

        await adapter._poll_health(mock_session)
        queue.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_first_poll_ok_then_ok_no_event(self) -> None:
        adapter, queue = _make_adapter()

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=_mock_response(200))

        await adapter._poll_health(mock_session)
        await adapter._poll_health(mock_session)
        queue.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_recovery_emits_event(self) -> None:
        adapter, queue = _make_adapter()
        adapter._service_ok = False

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=_mock_response(200))

        await adapter._poll_health(mock_session)

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "vaultwarden_recovered"
        assert event.severity == Severity.INFO
        assert event.system == "vaultwarden"
        assert event.source == "vaultwarden"

    @pytest.mark.asyncio
    async def test_dedup_key(self) -> None:
        adapter, queue = _make_adapter()
        adapter._service_ok = False

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=_mock_response(200))

        await adapter._poll_health(mock_session)

        event = queue.put_nowait.call_args[0][0]
        assert event.metadata.dedup_key == "vaultwarden:health"


# ---------------------------------------------------------------------------
# Failure handling — state transitions
# ---------------------------------------------------------------------------


class TestFailureHandling:
    def test_first_failure_emits_unreachable(self) -> None:
        adapter, queue = _make_adapter()

        adapter._handle_failure("connection refused")

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "vaultwarden_unreachable"
        assert event.severity == Severity.ERROR
        assert event.payload["reason"] == "connection refused"

    def test_transition_up_to_down_emits(self) -> None:
        adapter, queue = _make_adapter()
        adapter._service_ok = True

        adapter._handle_failure("timeout")

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "vaultwarden_unreachable"

    def test_already_down_no_duplicate(self) -> None:
        adapter, queue = _make_adapter()
        adapter._service_ok = False

        adapter._handle_failure("still down")

        queue.put_nowait.assert_not_called()

    def test_unreachable_dedup_key(self) -> None:
        adapter, queue = _make_adapter()

        adapter._handle_failure("refused")

        event = queue.put_nowait.call_args[0][0]
        assert event.metadata.dedup_key == "vaultwarden:health"

    def test_failure_resets_deep_health_state(self) -> None:
        adapter, _queue = _make_adapter(deep_health=True)
        adapter._service_ok = True
        adapter._api_ok = True
        adapter._is_slow = False

        adapter._handle_failure("connection lost")

        assert adapter._api_ok is None
        assert adapter._is_slow is None


# ---------------------------------------------------------------------------
# URL construction
# ---------------------------------------------------------------------------


class TestUrlConstruction:
    @pytest.mark.asyncio
    async def test_trailing_slash_stripped(self) -> None:
        adapter, _ = _make_adapter(url="http://localhost:8000/")

        mock_session = AsyncMock()
        mock_resp = _mock_response(200)
        mock_session.get = MagicMock(return_value=mock_resp)

        await adapter._poll_health(mock_session)

        called_url = mock_session.get.call_args[0][0]
        assert called_url == "http://localhost:8000/alive"


# ---------------------------------------------------------------------------
# Deep health — degraded detection
# ---------------------------------------------------------------------------


class TestDeepHealthDegraded:
    @pytest.mark.asyncio
    async def test_api_config_failure_emits_degraded(self) -> None:
        adapter, queue = _make_adapter(deep_health=True)

        mock_session = AsyncMock()
        fail_resp = AsyncMock()
        fail_resp.raise_for_status = MagicMock(
            side_effect=aiohttp.ClientResponseError(
                request_info=MagicMock(), history=(), status=500,
            ),
        )
        fail_resp.__aenter__ = AsyncMock(return_value=fail_resp)
        fail_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session.get = MagicMock(return_value=fail_resp)

        await adapter._poll_deep_health(mock_session)

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "vaultwarden_degraded"
        assert event.severity == Severity.WARNING
        assert event.metadata.dedup_key == "vaultwarden:degraded"
        assert adapter._api_ok is False

    @pytest.mark.asyncio
    async def test_degraded_no_duplicate(self) -> None:
        adapter, queue = _make_adapter(deep_health=True)
        adapter._api_ok = False  # already degraded

        mock_session = AsyncMock()
        fail_resp = AsyncMock()
        fail_resp.raise_for_status = MagicMock(
            side_effect=aiohttp.ClientResponseError(
                request_info=MagicMock(), history=(), status=500,
            ),
        )
        fail_resp.__aenter__ = AsyncMock(return_value=fail_resp)
        fail_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session.get = MagicMock(return_value=fail_resp)

        await adapter._poll_deep_health(mock_session)

        queue.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_api_config_ok_sets_state(self) -> None:
        adapter, queue = _make_adapter(deep_health=True)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=_mock_response(200))

        with patch("oasisagent.ingestion.vaultwarden.time.monotonic", side_effect=[0, 0.5]):
            await adapter._poll_deep_health(mock_session)

        assert adapter._api_ok is True
        queue.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_deep_health_not_called_when_disabled(self) -> None:
        adapter, _queue = _make_adapter(deep_health=False)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=_mock_response(200))

        await adapter._poll_health(mock_session)

        # Only /alive call, no /api/config
        assert mock_session.get.call_count == 1


# ---------------------------------------------------------------------------
# Deep health — slow detection
# ---------------------------------------------------------------------------


class TestDeepHealthSlow:
    @pytest.mark.asyncio
    async def test_slow_response_emits_event(self) -> None:
        adapter, queue = _make_adapter(deep_health=True, slow_threshold_ms=1000)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=_mock_response(200))

        # Simulate 1.5s response time
        with patch(
            "oasisagent.ingestion.vaultwarden.time.monotonic",
            side_effect=[0, 1.5],
        ):
            await adapter._poll_deep_health(mock_session)

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "vaultwarden_slow"
        assert event.severity == Severity.WARNING
        assert event.metadata.dedup_key == "vaultwarden:slow"
        assert event.payload["response_time_ms"] == 1500.0
        assert event.payload["threshold_ms"] == 1000
        assert adapter._is_slow is True

    @pytest.mark.asyncio
    async def test_slow_no_duplicate(self) -> None:
        adapter, queue = _make_adapter(deep_health=True, slow_threshold_ms=1000)
        adapter._is_slow = True  # already reported slow

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=_mock_response(200))

        with patch(
            "oasisagent.ingestion.vaultwarden.time.monotonic",
            side_effect=[0, 1.5],
        ):
            await adapter._poll_deep_health(mock_session)

        queue.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_fast_response_no_event(self) -> None:
        adapter, queue = _make_adapter(deep_health=True, slow_threshold_ms=1000)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=_mock_response(200))

        # Simulate 0.5s response time
        with patch(
            "oasisagent.ingestion.vaultwarden.time.monotonic",
            side_effect=[0, 0.5],
        ):
            await adapter._poll_deep_health(mock_session)

        queue.put_nowait.assert_not_called()
        assert adapter._is_slow is False

    @pytest.mark.asyncio
    async def test_slow_recovery_resets_state(self) -> None:
        adapter, queue = _make_adapter(deep_health=True, slow_threshold_ms=1000)
        adapter._is_slow = True  # was slow

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=_mock_response(200))

        # Now fast
        with patch(
            "oasisagent.ingestion.vaultwarden.time.monotonic",
            side_effect=[0, 0.1],
        ):
            await adapter._poll_deep_health(mock_session)

        assert adapter._is_slow is False
        queue.put_nowait.assert_not_called()


# ---------------------------------------------------------------------------
# Deep health integration with poll_health
# ---------------------------------------------------------------------------


class TestDeepHealthIntegration:
    @pytest.mark.asyncio
    async def test_poll_health_calls_deep_health_when_enabled(self) -> None:
        adapter, _queue = _make_adapter(deep_health=True)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=_mock_response(200))

        with patch(
            "oasisagent.ingestion.vaultwarden.time.monotonic",
            side_effect=[0, 0.1],
        ):
            await adapter._poll_health(mock_session)

        # /alive + /api/config
        assert mock_session.get.call_count == 2
        calls = [c[0][0] for c in mock_session.get.call_args_list]
        assert calls[0] == "http://localhost:8000/alive"
        assert calls[1] == "http://localhost:8000/api/config"


# ---------------------------------------------------------------------------
# Poll loop backoff
# ---------------------------------------------------------------------------


class TestPollLoopBackoff:
    @pytest.mark.asyncio
    async def test_backoff_increases_on_repeated_failures(self) -> None:
        """Poll loop uses exponential backoff when service is persistently down."""
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
            import aiohttp
            raise aiohttp.ClientError("connection refused")

        adapter._poll_health = _fail_poll  # type: ignore[method-assign]

        with patch(
            "oasisagent.ingestion.vaultwarden.aiohttp.ClientSession",
        ) as mock_cs, patch(
            "oasisagent.ingestion.vaultwarden.asyncio.sleep",
            side_effect=_tracking_sleep,
        ):
            mock_session = AsyncMock()
            mock_cs.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_cs.return_value.__aexit__ = AsyncMock(return_value=False)
            await adapter._poll_loop()

        # Each subsequent wait should be >= the previous (exponential backoff)
        assert len(sleep_totals) >= 2
        for i in range(1, len(sleep_totals)):
            assert sleep_totals[i] >= sleep_totals[i - 1]

    @pytest.mark.asyncio
    async def test_backoff_resets_on_success(self) -> None:
        """Backoff resets to poll_interval after a successful poll."""
        adapter, _ = _make_adapter(poll_interval=10)

        poll_count = 0
        sleep_totals: list[int] = []
        current_total = 0

        original_sleep = asyncio.sleep

        async def _tracking_sleep(_: float) -> None:
            nonlocal current_total
            current_total += 1
            await original_sleep(0)

        async def _poll_health(session: object) -> None:
            nonlocal poll_count, current_total
            if poll_count > 0:
                sleep_totals.append(current_total)
                current_total = 0
            poll_count += 1
            if poll_count == 1:
                import aiohttp
                raise aiohttp.ClientError("down")
            if poll_count >= 3:
                adapter._stopping = True
                return
            # Success on poll 2

        adapter._poll_health = _poll_health  # type: ignore[method-assign]

        with patch(
            "oasisagent.ingestion.vaultwarden.aiohttp.ClientSession",
        ) as mock_cs, patch(
            "oasisagent.ingestion.vaultwarden.asyncio.sleep",
            side_effect=_tracking_sleep,
        ):
            mock_session = AsyncMock()
            mock_cs.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_cs.return_value.__aexit__ = AsyncMock(return_value=False)
            await adapter._poll_loop()

        # After success, wait should be poll_interval (10), not backoff
        assert len(sleep_totals) >= 2
        assert sleep_totals[-1] == adapter._config.poll_interval


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_vaultwarden_connector_registered(self) -> None:
        from oasisagent.db.registry import CONNECTOR_TYPES

        assert "vaultwarden" in CONNECTOR_TYPES
        meta = CONNECTOR_TYPES["vaultwarden"]
        assert meta.model is VaultwardenAdapterConfig
        assert meta.secret_fields == frozenset({"admin_token"})

    def test_form_specs_exist(self) -> None:
        from oasisagent.ui.form_specs import FORM_SPECS

        assert "vaultwarden" in FORM_SPECS
        specs = FORM_SPECS["vaultwarden"]
        field_names = {s.name for s in specs}
        assert "url" in field_names
        assert "poll_interval" in field_names
        assert "timeout" in field_names
        assert "deep_health" in field_names
        assert "admin_token" in field_names
        assert "slow_threshold_ms" in field_names

    def test_display_name(self) -> None:
        from oasisagent.ui.form_specs import TYPE_DISPLAY_NAMES

        assert TYPE_DISPLAY_NAMES["vaultwarden"] == "Vaultwarden"

    def test_description(self) -> None:
        from oasisagent.ui.form_specs import TYPE_DESCRIPTIONS

        assert "vaultwarden" in TYPE_DESCRIPTIONS


# ---------------------------------------------------------------------------
# Known fixes
# ---------------------------------------------------------------------------


class TestKnownFixes:
    def test_known_fix_exists(self) -> None:
        from pathlib import Path

        import yaml

        fixes_path = Path(__file__).parent.parent.parent / "known_fixes" / "vaultwarden.yaml"
        assert fixes_path.exists(), f"Missing known fix: {fixes_path}"

        with fixes_path.open() as f:
            data = yaml.safe_load(f)

        fixes = data["fixes"]
        fix_ids = {fix["id"] for fix in fixes}
        assert "vaultwarden-unreachable" in fix_ids
        assert "vaultwarden-degraded" in fix_ids
        assert "vaultwarden-slow" in fix_ids

        unreachable = next(f for f in fixes if f["id"] == "vaultwarden-unreachable")
        assert unreachable["match"]["system"] == "vaultwarden"
        assert unreachable["match"]["event_type"] == "vaultwarden_unreachable"
        assert unreachable["risk_tier"] == "escalate"

    def test_degraded_fix_structure(self) -> None:
        from pathlib import Path

        import yaml

        fixes_path = Path(__file__).parent.parent.parent / "known_fixes" / "vaultwarden.yaml"
        with fixes_path.open() as f:
            data = yaml.safe_load(f)

        degraded = next(f for f in data["fixes"] if f["id"] == "vaultwarden-degraded")
        assert degraded["match"]["event_type"] == "vaultwarden_degraded"
        assert degraded["risk_tier"] == "escalate"

    def test_slow_fix_structure(self) -> None:
        from pathlib import Path

        import yaml

        fixes_path = Path(__file__).parent.parent.parent / "known_fixes" / "vaultwarden.yaml"
        with fixes_path.open() as f:
            data = yaml.safe_load(f)

        slow = next(f for f in data["fixes"] if f["id"] == "vaultwarden-slow")
        assert slow["match"]["event_type"] == "vaultwarden_slow"
        assert slow["risk_tier"] == "recommend"
