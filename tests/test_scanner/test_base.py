"""Tests for the ScannerIngestAdapter base class."""

from __future__ import annotations

import asyncio
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
    ) -> None:
        super().__init__(queue, interval)
        self._events_to_return = events or []
        self._error = error
        self.scan_count = 0

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
