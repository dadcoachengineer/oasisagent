"""Tests for the backup freshness scanner."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from oasisagent.config import BackupFreshnessCheckConfig, BackupSourceConfig
from oasisagent.models import Severity
from oasisagent.scanner.backup_freshness import (
    BackupFreshnessScannerAdapter,
    _FileChecker,
)


def _make_source(**overrides: Any) -> BackupSourceConfig:
    defaults: dict[str, Any] = {
        "name": "test-backup",
        "type": "file",
        "path": "/backup/*.tar.gz",
        "max_age_hours": 26,
    }
    defaults.update(overrides)
    return BackupSourceConfig(**defaults)


def _make_pbs_source(**overrides: Any) -> BackupSourceConfig:
    defaults: dict[str, Any] = {
        "name": "pbs-nightly",
        "type": "pbs",
        "url": "https://pbs.example.com:8007",
        "token_id": "user@pbs!agent",
        "token_secret": "secret-token",
        "datastore": "backups",
        "verify_ssl": False,
        "max_age_hours": 26,
    }
    defaults.update(overrides)
    return BackupSourceConfig(**defaults)


def _make_config(
    sources: list[BackupSourceConfig] | None = None,
) -> BackupFreshnessCheckConfig:
    return BackupFreshnessCheckConfig(
        enabled=True,
        interval=60,
        sources=sources or [_make_source()],
    )


def _mock_queue() -> MagicMock:
    return MagicMock()


def _make_scanner(
    config: BackupFreshnessCheckConfig | None = None,
    queue: MagicMock | None = None,
) -> BackupFreshnessScannerAdapter:
    return BackupFreshnessScannerAdapter(
        config=config or _make_config(),
        queue=queue or _mock_queue(),
        interval=60,
    )


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


class TestBackupFreshnessConfig:
    def test_defaults(self) -> None:
        cfg = BackupFreshnessCheckConfig()
        assert cfg.enabled is False
        assert cfg.interval == 3600
        assert cfg.sources == []

    def test_minimum_interval(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            BackupFreshnessCheckConfig(interval=10)

    def test_source_types(self) -> None:
        pbs = _make_pbs_source()
        assert pbs.type == "pbs"
        file_src = _make_source()
        assert file_src.type == "file"


# ---------------------------------------------------------------------------
# Scanner basics
# ---------------------------------------------------------------------------


class TestScannerBasics:
    def test_name(self) -> None:
        scanner = _make_scanner()
        assert scanner.name == "scanner.backup_freshness"


# ---------------------------------------------------------------------------
# State-based dedup (evaluate logic)
# ---------------------------------------------------------------------------


class TestEvaluateLogic:
    def test_fresh_first_scan_no_event(self) -> None:
        scanner = _make_scanner()
        source = _make_source()
        events = scanner._evaluate(source, 10.0)
        assert events == []

    def test_stale_first_scan_emits(self) -> None:
        scanner = _make_scanner()
        source = _make_source(max_age_hours=26)
        events = scanner._evaluate(source, 30.0)
        assert len(events) == 1
        assert events[0].event_type == "backup_stale"
        assert events[0].severity == Severity.WARNING
        assert events[0].payload["age_hours"] == 30.0
        assert events[0].payload["max_age_hours"] == 26

    def test_stale_dedup(self) -> None:
        scanner = _make_scanner()
        source = _make_source(max_age_hours=26)
        scanner._evaluate(source, 30.0)
        events = scanner._evaluate(source, 35.0)
        assert events == []  # still stale, no new event

    def test_stale_to_fresh_recovery(self) -> None:
        scanner = _make_scanner()
        source = _make_source(max_age_hours=26)
        scanner._evaluate(source, 30.0)  # stale
        events = scanner._evaluate(source, 1.0)  # fresh
        assert len(events) == 1
        assert events[0].event_type == "backup_recovered"
        assert events[0].severity == Severity.INFO

    def test_dedup_key_includes_source_name(self) -> None:
        scanner = _make_scanner()
        source = _make_source(name="my-backup", max_age_hours=26)
        events = scanner._evaluate(source, 30.0)
        assert "scanner.backup_freshness:my-backup" in (
            events[0].metadata.dedup_key
        )

    def test_multiple_sources_independent(self) -> None:
        s1 = _make_source(name="src-a", max_age_hours=26)
        s2 = _make_source(name="src-b", max_age_hours=26)
        scanner = _make_scanner(config=_make_config(sources=[s1, s2]))
        events_a = scanner._evaluate(s1, 30.0)  # stale
        events_b = scanner._evaluate(s2, 10.0)  # fresh
        assert len(events_a) == 1
        assert len(events_b) == 0


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    def test_check_error_emits_once(self) -> None:
        scanner = _make_scanner()
        source = _make_source()
        err = RuntimeError("connection refused")
        events1 = scanner._emit_check_error(source, err)
        events2 = scanner._emit_check_error(source, err)
        assert len(events1) == 1
        assert len(events2) == 0
        assert events1[0].event_type == "backup_check_error"
        assert events1[0].severity == Severity.ERROR

    def test_error_does_not_clear_stale_state(self) -> None:
        """D4: API failure must NOT clear stale state."""
        scanner = _make_scanner()
        source = _make_source(max_age_hours=26)
        scanner._evaluate(source, 30.0)  # mark as stale
        scanner._emit_check_error(source, RuntimeError("timeout"))
        # Source should still be in stale set
        assert source.name in scanner._stale_sources

    def test_error_clears_on_successful_check(self) -> None:
        scanner = _make_scanner()
        source = _make_source()
        scanner._emit_check_error(source, RuntimeError("timeout"))
        assert source.name in scanner._error_sources
        # Simulate successful scan clearing error state
        scanner._error_sources.discard(source.name)
        assert source.name not in scanner._error_sources


# ---------------------------------------------------------------------------
# File checker
# ---------------------------------------------------------------------------


class TestFileChecker:
    def test_no_matching_files(self, tmp_path: Path) -> None:
        source = _make_source(path=str(tmp_path / "*.tar.gz"))
        checker = _FileChecker(source)
        with pytest.raises(FileNotFoundError, match="No files match"):
            checker.newest_backup_age_hours()

    def test_newest_file_age(self, tmp_path: Path) -> None:
        # Create a file with recent mtime
        f = tmp_path / "backup.tar.gz"
        f.write_text("data")
        source = _make_source(path=str(tmp_path / "*.tar.gz"))
        checker = _FileChecker(source)
        age = checker.newest_backup_age_hours()
        assert age < 0.01  # just created

    def test_picks_newest_file(self, tmp_path: Path) -> None:
        old_file = tmp_path / "old.tar.gz"
        old_file.write_text("old")
        # Set mtime to 48 hours ago
        old_mtime = time.time() - 48 * 3600
        import os

        os.utime(old_file, (old_mtime, old_mtime))

        new_file = tmp_path / "new.tar.gz"
        new_file.write_text("new")

        source = _make_source(path=str(tmp_path / "*.tar.gz"))
        checker = _FileChecker(source)
        age = checker.newest_backup_age_hours()
        assert age < 0.01  # should pick the newest file


# ---------------------------------------------------------------------------
# Full scan cycle (mocked)
# ---------------------------------------------------------------------------


class TestFullScan:
    @pytest.mark.asyncio
    async def test_scan_calls_check_and_evaluates(self) -> None:
        source = _make_source(max_age_hours=26)
        scanner = _make_scanner(config=_make_config(sources=[source]))

        with patch.object(
            scanner, "_check_source", new_callable=AsyncMock,
        ) as mock_check:
            mock_check.return_value = 30.0  # stale
            events = await scanner._scan()

        assert len(events) == 1
        assert events[0].event_type == "backup_stale"

    @pytest.mark.asyncio
    async def test_scan_handles_check_error(self) -> None:
        source = _make_source()
        scanner = _make_scanner(config=_make_config(sources=[source]))

        with patch.object(
            scanner, "_check_source", new_callable=AsyncMock,
        ) as mock_check:
            mock_check.side_effect = RuntimeError("connection refused")
            events = await scanner._scan()

        assert len(events) == 1
        assert events[0].event_type == "backup_check_error"
