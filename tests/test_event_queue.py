"""Tests for the event queue with deduplication and backpressure."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime

import pytest

from oasisagent.engine.queue import EventQueue, QueueFullError
from oasisagent.models import Event, EventMetadata, Severity

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(
    entity_id: str = "automation.test",
    dedup_key: str = "",
    event_type: str = "automation_error",
) -> Event:
    """Create an Event with sensible defaults."""
    return Event(
        source="mqtt",
        system="homeassistant",
        event_type=event_type,
        entity_id=entity_id,
        severity=Severity.ERROR,
        timestamp=datetime(2026, 3, 7, 12, 0, 0, tzinfo=UTC),
        metadata=EventMetadata(dedup_key=dedup_key),
    )


# ---------------------------------------------------------------------------
# Basic put/get
# ---------------------------------------------------------------------------


class TestBasicOperations:
    """Tests for basic queue operations."""

    async def test_put_and_get(self) -> None:
        q = EventQueue(max_size=10)
        event = _make_event()
        result = await q.put(event)
        assert result is True
        assert q.size == 1

        got = await q.get()
        assert got.id == event.id
        assert q.size == 0

    async def test_fifo_order(self) -> None:
        q = EventQueue(max_size=10)
        e1 = _make_event(entity_id="first")
        e2 = _make_event(entity_id="second")
        e3 = _make_event(entity_id="third")

        await q.put(e1)
        await q.put(e2)
        await q.put(e3)

        assert (await q.get()).entity_id == "first"
        assert (await q.get()).entity_id == "second"
        assert (await q.get()).entity_id == "third"

    async def test_put_nowait(self) -> None:
        q = EventQueue(max_size=10)
        event = _make_event()
        result = q.put_nowait(event)
        assert result is True
        assert q.size == 1

    async def test_task_done(self) -> None:
        q = EventQueue(max_size=10)
        await q.put(_make_event())
        await q.get()
        q.task_done()  # Should not raise


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------


class TestProperties:
    """Tests for size, is_full, is_empty."""

    async def test_is_empty(self) -> None:
        q = EventQueue(max_size=5)
        assert q.is_empty is True
        await q.put(_make_event())
        assert q.is_empty is False

    async def test_is_full(self) -> None:
        q = EventQueue(max_size=2)
        assert q.is_full is False
        q.put_nowait(_make_event(entity_id="a"))
        q.put_nowait(_make_event(entity_id="b"))
        assert q.is_full is True

    async def test_size(self) -> None:
        q = EventQueue(max_size=10)
        assert q.size == 0
        await q.put(_make_event())
        assert q.size == 1
        await q.get()
        assert q.size == 0


# ---------------------------------------------------------------------------
# Backpressure
# ---------------------------------------------------------------------------


class TestBackpressure:
    """Tests for backpressure behavior."""

    async def test_put_nowait_raises_when_full(self) -> None:
        q = EventQueue(max_size=1)
        q.put_nowait(_make_event(entity_id="a"))

        with pytest.raises(QueueFullError, match="Event queue is full"):
            q.put_nowait(_make_event(entity_id="b"))

    async def test_queue_full_error_has_cause(self) -> None:
        q = EventQueue(max_size=1)
        q.put_nowait(_make_event())

        with pytest.raises(QueueFullError) as exc_info:
            q.put_nowait(_make_event(entity_id="b"))
        assert isinstance(exc_info.value.__cause__, asyncio.QueueFull)

    async def test_put_blocks_when_full_then_unblocks(self) -> None:
        q = EventQueue(max_size=1)
        await q.put(_make_event(entity_id="a"))
        assert q.is_full

        unblocked = asyncio.Event()

        async def _delayed_consume() -> None:
            await asyncio.sleep(0.05)
            await q.get()

        async def _blocked_put() -> None:
            await q.put(_make_event(entity_id="b"))
            unblocked.set()

        consumer = asyncio.create_task(_delayed_consume())
        producer = asyncio.create_task(_blocked_put())

        await asyncio.wait_for(unblocked.wait(), timeout=2.0)
        assert q.size == 1

        await consumer
        await producer


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------


class TestDeduplication:
    """Tests for dedup behavior."""

    async def test_duplicate_within_window_dropped(self) -> None:
        q = EventQueue(max_size=10, dedup_window_seconds=300)
        e1 = _make_event(dedup_key="mqtt:automation.test:error")
        e2 = _make_event(dedup_key="mqtt:automation.test:error")

        result1 = await q.put(e1)
        result2 = await q.put(e2)

        assert result1 is True
        assert result2 is False
        assert q.size == 1

    async def test_duplicate_put_nowait_dropped(self) -> None:
        q = EventQueue(max_size=10, dedup_window_seconds=300)
        e1 = _make_event(dedup_key="key1")
        e2 = _make_event(dedup_key="key1")

        result1 = q.put_nowait(e1)
        result2 = q.put_nowait(e2)

        assert result1 is True
        assert result2 is False
        assert q.size == 1

    async def test_different_keys_not_deduped(self) -> None:
        q = EventQueue(max_size=10, dedup_window_seconds=300)
        e1 = _make_event(dedup_key="key1")
        e2 = _make_event(dedup_key="key2")

        assert await q.put(e1) is True
        assert await q.put(e2) is True
        assert q.size == 2

    async def test_empty_dedup_key_not_deduped(self) -> None:
        q = EventQueue(max_size=10, dedup_window_seconds=300)
        e1 = _make_event(dedup_key="")
        e2 = _make_event(dedup_key="")

        assert await q.put(e1) is True
        assert await q.put(e2) is True
        assert q.size == 2

    async def test_dedup_disabled_when_window_zero(self) -> None:
        q = EventQueue(max_size=10, dedup_window_seconds=0)
        e1 = _make_event(dedup_key="same-key")
        e2 = _make_event(dedup_key="same-key")

        assert await q.put(e1) is True
        assert await q.put(e2) is True
        assert q.size == 2

    async def test_duplicate_after_window_expires_allowed(self) -> None:
        q = EventQueue(max_size=10, dedup_window_seconds=1)
        e1 = _make_event(dedup_key="key1")

        assert await q.put(e1) is True

        # Fake time advancement by manipulating the seen timestamp
        q._seen["key1"] = time.monotonic() - 2  # 2 seconds ago, past the 1s window

        e2 = _make_event(dedup_key="key1")
        assert await q.put(e2) is True
        assert q.size == 2

    async def test_dedup_cache_pruning(self) -> None:
        """Verify expired dedup entries are actually cleaned up."""
        q = EventQueue(max_size=10, dedup_window_seconds=1)
        q._PRUNE_INTERVAL_SECONDS = 0  # Force prune on every check

        e1 = _make_event(dedup_key="prune-me")
        await q.put(e1)
        assert q.dedup_cache_size == 1

        # Age the entry past the window
        q._seen["prune-me"] = time.monotonic() - 2
        q._last_prune = 0  # Force prune on next put

        # Put a different event to trigger pruning
        e2 = _make_event(dedup_key="trigger-prune")
        await q.put(e2)

        assert "prune-me" not in q._seen
        assert q.dedup_cache_size == 1  # Only "trigger-prune" remains

    async def test_dedup_logs_debug(self, caplog: pytest.LogCaptureFixture) -> None:
        q = EventQueue(max_size=10, dedup_window_seconds=300)
        e1 = _make_event(dedup_key="logged-key")
        e2 = _make_event(dedup_key="logged-key")

        with caplog.at_level(logging.DEBUG):
            await q.put(e1)
            await q.put(e2)

        assert any("Dedup: dropping" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Drain
# ---------------------------------------------------------------------------


class TestDrain:
    """Tests for drain() during shutdown."""

    async def test_drain_returns_all_events(self) -> None:
        q = EventQueue(max_size=10)
        await q.put(_make_event(entity_id="a"))
        await q.put(_make_event(entity_id="b"))
        await q.put(_make_event(entity_id="c"))

        drained = q.drain()
        assert len(drained) == 3
        assert q.is_empty

    async def test_drain_empty_queue(self) -> None:
        q = EventQueue(max_size=10)
        assert q.drain() == []

    async def test_drain_preserves_order(self) -> None:
        q = EventQueue(max_size=10)
        await q.put(_make_event(entity_id="first"))
        await q.put(_make_event(entity_id="second"))

        drained = q.drain()
        assert drained[0].entity_id == "first"
        assert drained[1].entity_id == "second"


# ---------------------------------------------------------------------------
# Capacity warnings (hysteresis)
# ---------------------------------------------------------------------------


class TestCapacityWarnings:
    """Tests for the 80% warning with hysteresis."""

    async def test_warning_at_80_percent(self, caplog: pytest.LogCaptureFixture) -> None:
        q = EventQueue(max_size=5)

        with caplog.at_level(logging.WARNING):
            for i in range(4):
                q.put_nowait(_make_event(entity_id=f"e{i}"))

        assert any("capacity" in r.message.lower() for r in caplog.records)

    async def test_warning_fires_only_once(self, caplog: pytest.LogCaptureFixture) -> None:
        q = EventQueue(max_size=5)

        with caplog.at_level(logging.WARNING):
            for i in range(5):
                q.put_nowait(_make_event(entity_id=f"e{i}"))

        warning_count = sum(1 for r in caplog.records if "capacity" in r.message.lower())
        assert warning_count == 1

    async def test_warning_resets_after_recovery(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        q = EventQueue(max_size=10)

        # Fill to 80%
        with caplog.at_level(logging.WARNING):
            for i in range(8):
                q.put_nowait(_make_event(entity_id=f"e{i}"))

        first_warnings = sum(1 for r in caplog.records if "capacity" in r.message.lower())
        assert first_warnings == 1
        assert q._capacity_warning_active is True

        # Drain to below 50%
        caplog.clear()
        with caplog.at_level(logging.INFO):
            for _ in range(5):
                await q.get()

        assert q._capacity_warning_active is False
        assert any("recovered" in r.message.lower() for r in caplog.records)

        # Fill again — should warn again
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            for i in range(5):
                q.put_nowait(_make_event(entity_id=f"refill{i}"))

        second_warnings = sum(1 for r in caplog.records if "capacity" in r.message.lower())
        assert second_warnings == 1
