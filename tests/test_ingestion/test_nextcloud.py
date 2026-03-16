"""Tests for the Nextcloud server ingestion adapter."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from pydantic import ValidationError

from oasisagent.config import NextcloudAdapterConfig
from oasisagent.ingestion.nextcloud import NextcloudAdapter
from oasisagent.models import Severity


def _make_config(**overrides: object) -> NextcloudAdapterConfig:
    defaults: dict[str, object] = {
        "enabled": True,
        "url": "https://nextcloud.example.com",
        "username": "admin",
        "password": "app-password",
        "poll_interval": 60,
        "timeout": 10,
    }
    defaults.update(overrides)
    return NextcloudAdapterConfig(**defaults)  # type: ignore[arg-type]


def _mock_queue() -> MagicMock:
    return MagicMock()


def _make_adapter(**overrides: object) -> tuple[NextcloudAdapter, MagicMock]:
    config = _make_config(**overrides)
    queue = _mock_queue()
    adapter = NextcloudAdapter(config, queue)
    return adapter, queue


def _server_info(
    *,
    maintenance: bool = False,
    last_cron: int | None = None,
) -> dict:
    """Build a minimal OCS server info response."""
    system: dict = {}
    if maintenance:
        system["maintenance"] = True
    if last_cron is not None:
        system["last_cron"] = last_cron
    return {
        "ocs": {
            "data": {
                "nextcloud": {"system": system},
                "server": {"php": {"opcache": {}}},
            },
        },
    }


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
        config = NextcloudAdapterConfig()
        assert config.enabled is False
        assert config.username == ""
        assert config.password == ""

    def test_extra_fields_forbidden(self) -> None:
        with pytest.raises(ValidationError, match="Extra inputs"):
            _make_config(bogus="nope")


# ---------------------------------------------------------------------------
# Adapter identity & lifecycle
# ---------------------------------------------------------------------------


class TestAdapterLifecycle:
    def test_name(self) -> None:
        adapter, _ = _make_adapter()
        assert adapter.name == "nextcloud"

    @pytest.mark.asyncio
    async def test_healthy_initially_false(self) -> None:
        adapter, _ = _make_adapter()
        assert not await adapter.healthy()


# ---------------------------------------------------------------------------
# Health polling
# ---------------------------------------------------------------------------


class TestHealthPolling:
    @pytest.mark.asyncio
    async def test_first_poll_ok_no_event(self) -> None:
        adapter, queue = _make_adapter()

        now_ts = int(time.time())
        mock_session = AsyncMock()
        mock_session.get = MagicMock(
            return_value=_mock_response(200, _server_info(last_cron=now_ts)),
        )

        await adapter._poll_health(mock_session)
        queue.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_recovery_emits_event(self) -> None:
        adapter, queue = _make_adapter()
        adapter._service_ok = False

        now_ts = int(time.time())
        mock_session = AsyncMock()
        mock_session.get = MagicMock(
            return_value=_mock_response(200, _server_info(last_cron=now_ts)),
        )

        await adapter._poll_health(mock_session)

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "nextcloud_recovered"
        assert event.severity == Severity.INFO

    @pytest.mark.asyncio
    async def test_url_construction(self) -> None:
        adapter, _ = _make_adapter(url="https://nextcloud.example.com/")

        mock_session = AsyncMock()
        mock_session.get = MagicMock(
            return_value=_mock_response(200, _server_info()),
        )

        await adapter._poll_health(mock_session)

        called_url = mock_session.get.call_args[0][0]
        assert "ocs/v2.php/apps/serverinfo" in called_url
        assert not called_url.startswith("https://nextcloud.example.com//")

    @pytest.mark.asyncio
    async def test_ocs_header_sent(self) -> None:
        adapter, _ = _make_adapter()

        mock_session = AsyncMock()
        mock_session.get = MagicMock(
            return_value=_mock_response(200, _server_info()),
        )

        await adapter._poll_health(mock_session)

        call_kwargs = mock_session.get.call_args
        assert call_kwargs[1]["headers"]["OCS-APIREQUEST"] == "true"


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------


class TestFailureHandling:
    def test_first_failure_emits_unreachable(self) -> None:
        adapter, queue = _make_adapter()

        adapter._handle_failure("connection refused")

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "nextcloud_unreachable"
        assert event.severity == Severity.ERROR

    def test_already_down_no_duplicate(self) -> None:
        adapter, queue = _make_adapter()
        adapter._service_ok = False

        adapter._handle_failure("still down")
        queue.put_nowait.assert_not_called()

    def test_failure_resets_state(self) -> None:
        adapter, _queue = _make_adapter()
        adapter._service_ok = True
        adapter._cron_stale = True
        adapter._maintenance = True

        adapter._handle_failure("lost")

        assert adapter._cron_stale is None
        assert adapter._maintenance is None


# ---------------------------------------------------------------------------
# Cron monitoring
# ---------------------------------------------------------------------------


class TestCronMonitoring:
    def test_stale_cron_emits_event(self) -> None:
        adapter, queue = _make_adapter()

        # Cron ran 2 hours ago (stale)
        old_ts = int(time.time()) - 7200
        adapter._check_cron(old_ts)

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "nextcloud_cron_stale"
        assert event.severity == Severity.WARNING

    def test_fresh_cron_no_event(self) -> None:
        adapter, queue = _make_adapter()

        # Cron ran 5 minutes ago (fresh)
        recent_ts = int(time.time()) - 300
        adapter._check_cron(recent_ts)

        queue.put_nowait.assert_not_called()
        assert adapter._cron_stale is False

    def test_stale_cron_no_duplicate(self) -> None:
        adapter, queue = _make_adapter()
        adapter._cron_stale = True

        old_ts = int(time.time()) - 7200
        adapter._check_cron(old_ts)

        queue.put_nowait.assert_not_called()


# ---------------------------------------------------------------------------
# Maintenance mode
# ---------------------------------------------------------------------------


class TestMaintenanceMode:
    def test_maintenance_mode_emits_event(self) -> None:
        adapter, queue = _make_adapter()

        adapter._check_maintenance({"maintenance": True})

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "nextcloud_maintenance_mode"
        assert event.severity == Severity.WARNING

    def test_no_maintenance_no_event(self) -> None:
        adapter, queue = _make_adapter()

        adapter._check_maintenance({"maintenance": False})

        queue.put_nowait.assert_not_called()

    def test_maintenance_no_duplicate(self) -> None:
        adapter, queue = _make_adapter()
        adapter._maintenance = True

        adapter._check_maintenance({"maintenance": True})

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
            "oasisagent.ingestion.nextcloud.aiohttp.ClientSession",
        ) as mock_cs, patch(
            "oasisagent.ingestion.nextcloud.asyncio.sleep",
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
    def test_nextcloud_connector_registered(self) -> None:
        from oasisagent.db.registry import CONNECTOR_TYPES

        assert "nextcloud" in CONNECTOR_TYPES
        meta = CONNECTOR_TYPES["nextcloud"]
        assert meta.model is NextcloudAdapterConfig
        assert meta.secret_fields == frozenset({"password"})

    def test_form_specs_exist(self) -> None:
        from oasisagent.ui.form_specs import FORM_SPECS

        assert "nextcloud" in FORM_SPECS
        field_names = {s.name for s in FORM_SPECS["nextcloud"]}
        assert "url" in field_names
        assert "username" in field_names
        assert "password" in field_names

    def test_display_name(self) -> None:
        from oasisagent.ui.form_specs import TYPE_DISPLAY_NAMES

        assert TYPE_DISPLAY_NAMES["nextcloud"] == "Nextcloud"


# ---------------------------------------------------------------------------
# Known fixes
# ---------------------------------------------------------------------------


class TestKnownFixes:
    def test_known_fixes_exist(self) -> None:
        from pathlib import Path

        import yaml

        fixes_path = Path(__file__).parent.parent.parent / "known_fixes" / "nextcloud.yaml"
        assert fixes_path.exists()

        with fixes_path.open() as f:
            data = yaml.safe_load(f)

        fix_ids = {fix["id"] for fix in data["fixes"]}
        assert "nextcloud-unreachable" in fix_ids
        assert "nextcloud-cron-stale" in fix_ids
        assert "nextcloud-maintenance-mode" in fix_ids
