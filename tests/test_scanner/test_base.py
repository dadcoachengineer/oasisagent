"""Tests for the ScannerIngestAdapter base class."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest

from oasisagent.models import Event, EventMetadata, Severity
from oasisagent.scanner.base import ScannerIngestAdapter


class _StubScanner(ScannerIngestAdapter):
    """Minimal scanner for testing the base class poll loop."""

    def __init__(
        self,
        queue: MagicMock,
        interval: int = 1,
        events: list[Event] | None = None,
        error: Exception | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(queue, interval, **kwargs)
        self._events_to_return = events or []
        self._error = error
        self.scan_count = 0

    def set_events(self, events: list[Event]) -> None:
        """Update events returned by subsequent scans."""
        self._events_to_return = events

    @property
    def name(self) -> str:
        return "scanner.stub"

    async def _scan(self) -> list[Event]:
        self.scan_count += 1
        if self._error:
            raise self._error
        return self._events_to_return


def _make_event(**overrides: object) -> Event:
    from datetime import UTC, datetime

    defaults: dict = {
        "source": "scanner.stub",
        "system": "test",
        "event_type": "test_event",
        "entity_id": "test-1",
        "severity": Severity.WARNING,
        "timestamp": datetime.now(tz=UTC),
        "metadata": EventMetadata(dedup_key="test:1"),
    }
    defaults.update(overrides)
    return Event(**defaults)


def _mock_queue() -> MagicMock:
    return MagicMock()


class TestScannerBase:
    def test_name_property(self) -> None:
        scanner = _StubScanner(_mock_queue())
        assert scanner.name == "scanner.stub"

    @pytest.mark.asyncio
    async def test_poll_loop_calls_scan(self) -> None:
        """Verify the poll loop invokes _scan and enqueues returned events."""
        event = _make_event()
        queue = _mock_queue()
        scanner = _StubScanner(queue, interval=1, events=[event])

        # Run one cycle then stop
        async def _stop_after_scan() -> None:
            while scanner.scan_count < 1:
                await asyncio.sleep(0.05)
            await scanner.stop()

        task = asyncio.create_task(scanner.start())
        stop_task = asyncio.create_task(_stop_after_scan())
        await asyncio.wait([task, stop_task], timeout=5)

        assert scanner.scan_count >= 1
        queue.put_nowait.assert_called_with(event)

    @pytest.mark.asyncio
    async def test_scan_error_sets_unhealthy(self) -> None:
        """A scan error should mark the scanner as unhealthy."""
        queue = _mock_queue()
        scanner = _StubScanner(queue, interval=1, error=RuntimeError("boom"))

        async def _stop_after_scan() -> None:
            while scanner.scan_count < 1:
                await asyncio.sleep(0.05)
            await scanner.stop()

        task = asyncio.create_task(scanner.start())
        stop_task = asyncio.create_task(_stop_after_scan())
        await asyncio.wait([task, stop_task], timeout=5)

        assert not await scanner.healthy()
        queue.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_healthy_after_successful_scan(self) -> None:
        queue = _mock_queue()
        scanner = _StubScanner(queue, interval=1, events=[])

        async def _stop_after_scan() -> None:
            while scanner.scan_count < 1:
                await asyncio.sleep(0.05)
            await scanner.stop()

        task = asyncio.create_task(scanner.start())
        stop_task = asyncio.create_task(_stop_after_scan())
        await asyncio.wait([task, stop_task], timeout=5)

        assert await scanner.healthy()

    @pytest.mark.asyncio
    async def test_stop_cancels_task(self) -> None:
        queue = _mock_queue()
        scanner = _StubScanner(queue, interval=60, events=[])

        task = asyncio.create_task(scanner.start())
        await asyncio.sleep(0.1)
        await scanner.stop()

        # Task should complete after cancellation
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)

    def test_enqueue_failure_logged(self) -> None:
        """If put_nowait raises, _enqueue logs but doesn't propagate."""
        queue = _mock_queue()
        queue.put_nowait.side_effect = RuntimeError("queue full")
        scanner = _StubScanner(queue, interval=1)

        event = _make_event()
        # Should not raise
        scanner._enqueue(event)
        queue.put_nowait.assert_called_once_with(event)


# ---------------------------------------------------------------------------
# Adaptive interval tests
# ---------------------------------------------------------------------------


class TestAdaptiveIntervals:
    def test_effective_interval_normal(self) -> None:
        """Without issues, effective interval equals configured interval."""
        scanner = _StubScanner(_mock_queue(), interval=900)
        assert scanner._effective_interval == 900

    def test_effective_interval_fast_mode(self) -> None:
        """After detecting issues, interval is reduced by fast factor."""
        scanner = _StubScanner(
            _mock_queue(), interval=900,
            adaptive_enabled=True, adaptive_fast_factor=0.25,
        )
        scanner._adapted = True
        assert scanner._effective_interval == 225

    def test_effective_interval_minimum_60(self) -> None:
        """Fast mode interval is clamped to at least 60 seconds."""
        scanner = _StubScanner(
            _mock_queue(), interval=100,
            adaptive_enabled=True, adaptive_fast_factor=0.1,
        )
        scanner._adapted = True
        # 100 * 0.1 = 10, but should be clamped to 60
        assert scanner._effective_interval == 60

    def test_adaptive_disabled(self) -> None:
        """When adaptive is disabled, interval never changes."""
        scanner = _StubScanner(
            _mock_queue(), interval=900,
            adaptive_enabled=False,
        )
        warning_event = _make_event(severity=Severity.WARNING)
        scanner._update_adaptive_state([warning_event])
        assert scanner._effective_interval == 900
        assert scanner._adapted is False

    def test_warning_event_enters_fast_mode(self) -> None:
        """WARNING+ events should trigger fast mode."""
        scanner = _StubScanner(_mock_queue(), interval=900)
        warning_event = _make_event(severity=Severity.WARNING)
        scanner._update_adaptive_state([warning_event])
        assert scanner._adapted is True
        assert scanner._consecutive_clean == 0

    def test_info_event_does_not_enter_fast_mode(self) -> None:
        """INFO events (recovery) should not trigger fast mode."""
        scanner = _StubScanner(_mock_queue(), interval=900)
        info_event = _make_event(severity=Severity.INFO)
        scanner._update_adaptive_state([info_event])
        assert scanner._adapted is False

    def test_clean_scans_restore_normal(self) -> None:
        """After enough clean scans, fast mode is exited."""
        scanner = _StubScanner(
            _mock_queue(), interval=900,
            adaptive_recovery_scans=3,
        )
        # Enter fast mode
        warning_event = _make_event(severity=Severity.WARNING)
        scanner._update_adaptive_state([warning_event])
        assert scanner._adapted is True

        # 2 clean scans — still in fast mode
        scanner._update_adaptive_state([])
        assert scanner._adapted is True
        assert scanner._consecutive_clean == 1
        scanner._update_adaptive_state([])
        assert scanner._adapted is True
        assert scanner._consecutive_clean == 2

        # 3rd clean scan — exits fast mode
        scanner._update_adaptive_state([])
        assert scanner._adapted is False
        assert scanner._consecutive_clean == 0

    def test_issue_during_recovery_resets_count(self) -> None:
        """A new issue during recovery resets the clean scan counter."""
        scanner = _StubScanner(
            _mock_queue(), interval=900,
            adaptive_recovery_scans=3,
        )
        warning_event = _make_event(severity=Severity.WARNING)

        # Enter fast mode and start recovering
        scanner._update_adaptive_state([warning_event])
        scanner._update_adaptive_state([])  # clean 1
        scanner._update_adaptive_state([])  # clean 2

        # New issue resets counter
        scanner._update_adaptive_state([warning_event])
        assert scanner._adapted is True
        assert scanner._consecutive_clean == 0

    def test_error_severity_enters_fast_mode(self) -> None:
        """ERROR severity should also trigger fast mode."""
        scanner = _StubScanner(_mock_queue(), interval=900)
        error_event = _make_event(severity=Severity.ERROR)
        scanner._update_adaptive_state([error_event])
        assert scanner._adapted is True

    def test_mixed_info_and_warning_enters_fast_mode(self) -> None:
        """If any event is WARNING+, fast mode is triggered."""
        scanner = _StubScanner(_mock_queue(), interval=900)
        events = [
            _make_event(severity=Severity.INFO),
            _make_event(severity=Severity.WARNING),
        ]
        scanner._update_adaptive_state(events)
        assert scanner._adapted is True
