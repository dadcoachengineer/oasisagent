"""Tests for the disk space usage scanner."""

from __future__ import annotations

import shutil
from collections import namedtuple
from unittest.mock import MagicMock, patch

import pytest

from oasisagent.config import DiskSpaceCheckConfig
from oasisagent.models import Severity
from oasisagent.scanner.disk_space import DiskSpaceScannerAdapter


def _make_config(**overrides: object) -> DiskSpaceCheckConfig:
    defaults: dict = {
        "enabled": True,
        "paths": ["/tmp"],
        "warning_threshold_pct": 85,
        "critical_threshold_pct": 95,
        "interval": 60,
    }
    defaults.update(overrides)
    return DiskSpaceCheckConfig(**defaults)


def _mock_queue() -> MagicMock:
    return MagicMock()


def _make_scanner(
    config: DiskSpaceCheckConfig | None = None,
    queue: MagicMock | None = None,
) -> DiskSpaceScannerAdapter:
    return DiskSpaceScannerAdapter(
        config=config or _make_config(),
        queue=queue or _mock_queue(),
        interval=60,
    )


# Named tuple matching shutil.disk_usage return type
_DiskUsage = namedtuple("usage", ["total", "used", "free"])


def _gb(n: float) -> int:
    """Convert GB to bytes."""
    return int(n * (1024**3))


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


class TestDiskSpaceConfig:
    def test_defaults(self) -> None:
        cfg = DiskSpaceCheckConfig()
        assert cfg.enabled is False
        assert cfg.warning_threshold_pct == 85
        assert cfg.critical_threshold_pct == 95
        assert cfg.interval == 900

    def test_minimum_interval(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            DiskSpaceCheckConfig(interval=10)


# ---------------------------------------------------------------------------
# Name property
# ---------------------------------------------------------------------------


class TestDiskSpaceScanner:
    def test_name(self) -> None:
        scanner = _make_scanner()
        assert scanner.name == "scanner.disk_space"


# ---------------------------------------------------------------------------
# State-based dedup (evaluate logic)
# ---------------------------------------------------------------------------


class TestDiskSpaceEvaluate:
    def test_first_scan_ok_no_event(self) -> None:
        scanner = _make_scanner()
        usage = _DiskUsage(total=_gb(100), used=_gb(50), free=_gb(50))
        events = scanner._evaluate("/tmp", usage, 50.0)
        assert events == []

    def test_first_scan_warning_emits(self) -> None:
        scanner = _make_scanner()
        usage = _DiskUsage(total=_gb(100), used=_gb(90), free=_gb(10))
        events = scanner._evaluate("/tmp", usage, 90.0)
        assert len(events) == 1
        assert events[0].event_type == "disk_space_warning"
        assert events[0].severity == Severity.WARNING
        assert events[0].payload["used_pct"] == 90.0

    def test_first_scan_critical_emits(self) -> None:
        scanner = _make_scanner()
        usage = _DiskUsage(total=_gb(100), used=_gb(97), free=_gb(3))
        events = scanner._evaluate("/tmp", usage, 97.0)
        assert len(events) == 1
        assert events[0].event_type == "disk_space_critical"
        assert events[0].severity == Severity.ERROR

    def test_same_state_dedup(self) -> None:
        scanner = _make_scanner()
        usage = _DiskUsage(total=_gb(100), used=_gb(90), free=_gb(10))
        events1 = scanner._evaluate("/tmp", usage, 90.0)
        events2 = scanner._evaluate("/tmp", usage, 91.0)
        assert len(events1) == 1
        assert len(events2) == 0  # still warning

    def test_warning_to_critical_emits(self) -> None:
        scanner = _make_scanner()
        usage_w = _DiskUsage(total=_gb(100), used=_gb(90), free=_gb(10))
        usage_c = _DiskUsage(total=_gb(100), used=_gb(97), free=_gb(3))
        scanner._evaluate("/tmp", usage_w, 90.0)  # warning
        events = scanner._evaluate("/tmp", usage_c, 97.0)  # -> critical
        assert len(events) == 1
        assert events[0].event_type == "disk_space_critical"

    def test_critical_to_ok_emits_recovery(self) -> None:
        scanner = _make_scanner()
        usage_c = _DiskUsage(total=_gb(100), used=_gb(97), free=_gb(3))
        usage_ok = _DiskUsage(total=_gb(100), used=_gb(50), free=_gb(50))
        scanner._evaluate("/tmp", usage_c, 97.0)  # critical
        events = scanner._evaluate("/tmp", usage_ok, 50.0)  # -> ok
        assert len(events) == 1
        assert events[0].event_type == "disk_space_recovered"
        assert events[0].severity == Severity.INFO
        assert events[0].payload["previous_state"] == "critical"

    def test_warning_to_ok_emits_recovery(self) -> None:
        scanner = _make_scanner()
        usage_w = _DiskUsage(total=_gb(100), used=_gb(90), free=_gb(10))
        usage_ok = _DiskUsage(total=_gb(100), used=_gb(50), free=_gb(50))
        scanner._evaluate("/tmp", usage_w, 90.0)  # warning
        events = scanner._evaluate("/tmp", usage_ok, 50.0)  # -> ok
        assert len(events) == 1
        assert events[0].event_type == "disk_space_recovered"

    def test_dedup_key_includes_path(self) -> None:
        scanner = _make_scanner()
        usage = _DiskUsage(total=_gb(100), used=_gb(97), free=_gb(3))
        events = scanner._evaluate("/tmp", usage, 97.0)
        assert events[0].metadata.dedup_key == "scanner.disk_space:/tmp"

    def test_payload_includes_disk_stats(self) -> None:
        scanner = _make_scanner()
        usage = _DiskUsage(total=_gb(100), used=_gb(90), free=_gb(10))
        events = scanner._evaluate("/tmp", usage, 90.0)
        payload = events[0].payload
        assert payload["total_gb"] == 100.0
        assert payload["used_gb"] == 90.0
        assert payload["free_gb"] == 10.0
        assert payload["used_pct"] == 90.0

    def test_multiple_paths_tracked_independently(self) -> None:
        cfg = _make_config(paths=["/tmp", "/var"])
        scanner = _make_scanner(config=cfg)
        usage_w = _DiskUsage(total=_gb(100), used=_gb(90), free=_gb(10))
        usage_ok = _DiskUsage(total=_gb(100), used=_gb(50), free=_gb(50))
        events_tmp = scanner._evaluate("/tmp", usage_w, 90.0)
        events_var = scanner._evaluate("/var", usage_ok, 50.0)
        assert len(events_tmp) == 1  # warning
        assert len(events_var) == 0  # ok, first scan


# ---------------------------------------------------------------------------
# Full scan with mocked shutil
# ---------------------------------------------------------------------------


class TestDiskSpaceScan:
    @pytest.mark.asyncio
    async def test_scan_calls_disk_usage(self) -> None:
        scanner = _make_scanner()
        usage = _DiskUsage(total=_gb(100), used=_gb(50), free=_gb(50))
        with patch.object(shutil, "disk_usage", return_value=usage):
            events = await scanner._scan()
        # First scan at 50% — no event
        assert events == []

    @pytest.mark.asyncio
    async def test_scan_emits_on_threshold(self) -> None:
        scanner = _make_scanner()
        usage = _DiskUsage(total=_gb(100), used=_gb(90), free=_gb(10))
        with patch.object(shutil, "disk_usage", return_value=usage):
            events = await scanner._scan()
        assert len(events) == 1
        assert events[0].event_type == "disk_space_warning"

    @pytest.mark.asyncio
    async def test_scan_error_emits_check_error(self) -> None:
        scanner = _make_scanner()
        with patch.object(shutil, "disk_usage", side_effect=OSError("no such path")):
            events = await scanner._scan()
        assert len(events) == 1
        assert events[0].event_type == "disk_check_error"


# ---------------------------------------------------------------------------
# Check error handling
# ---------------------------------------------------------------------------


class TestDiskSpaceErrors:
    def test_check_error_emits_once(self) -> None:
        scanner = _make_scanner()
        events1 = scanner._emit_check_error("/tmp", OSError("permission denied"))
        events2 = scanner._emit_check_error("/tmp", OSError("permission denied"))
        assert len(events1) == 1
        assert len(events2) == 0  # deduped

    def test_check_error_after_ok_emits(self) -> None:
        scanner = _make_scanner()
        usage = _DiskUsage(total=_gb(100), used=_gb(50), free=_gb(50))
        scanner._evaluate("/tmp", usage, 50.0)  # ok
        events = scanner._emit_check_error("/tmp", OSError("fail"))
        assert len(events) == 1
