"""Tests for the stats store (dashboard counter persistence)."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from oasisagent.db.schema import run_migrations
from oasisagent.db.stats_store import StatsStore

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    import aiosqlite


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def db(tmp_path: Path) -> AsyncGenerator[aiosqlite.Connection]:
    """Create a migrated SQLite database."""
    conn = await run_migrations(tmp_path / "test.db")
    yield conn
    await conn.close()


# ---------------------------------------------------------------------------
# from_db / load
# ---------------------------------------------------------------------------


class TestStatsStoreLoad:
    """Loading stats from a fresh and pre-populated database."""

    @pytest.mark.asyncio
    async def test_fresh_db_returns_zeros(self, db: aiosqlite.Connection) -> None:
        store = await StatsStore.from_db(db)
        vals = store.values
        assert vals == {"events_processed": 0, "actions_taken": 0, "errors": 0}

    @pytest.mark.asyncio
    async def test_loads_persisted_values(self, db: aiosqlite.Connection) -> None:
        await db.execute("UPDATE stats SET value = 42 WHERE key = 'events_processed'")
        await db.execute("UPDATE stats SET value = 7 WHERE key = 'actions_taken'")
        await db.execute("UPDATE stats SET value = 3 WHERE key = 'errors'")
        await db.commit()

        store = await StatsStore.from_db(db)
        vals = store.values
        assert vals == {"events_processed": 42, "actions_taken": 7, "errors": 3}


# ---------------------------------------------------------------------------
# flush
# ---------------------------------------------------------------------------


class TestStatsStoreFlush:
    """Writing stats to SQLite."""

    @pytest.mark.asyncio
    async def test_flush_persists_values(self, db: aiosqlite.Connection) -> None:
        store = await StatsStore.from_db(db)
        await store.flush(events_processed=10, actions_taken=5, errors=1)

        cursor = await db.execute("SELECT key, value FROM stats ORDER BY key")
        rows = {r["key"]: r["value"] for r in await cursor.fetchall()}
        assert rows == {"actions_taken": 5, "errors": 1, "events_processed": 10}

    @pytest.mark.asyncio
    async def test_flush_updates_in_memory_values(self, db: aiosqlite.Connection) -> None:
        store = await StatsStore.from_db(db)
        await store.flush(events_processed=10, actions_taken=5, errors=1)
        assert store.values == {"events_processed": 10, "actions_taken": 5, "errors": 1}

    @pytest.mark.asyncio
    async def test_flush_overwrites_previous(self, db: aiosqlite.Connection) -> None:
        store = await StatsStore.from_db(db)
        await store.flush(events_processed=10, actions_taken=5, errors=1)
        await store.flush(events_processed=20, actions_taken=8, errors=2)

        cursor = await db.execute("SELECT value FROM stats WHERE key = 'events_processed'")
        row = await cursor.fetchone()
        assert row["value"] == 20


# ---------------------------------------------------------------------------
# Restart roundtrip
# ---------------------------------------------------------------------------


class TestStatsStoreRoundtrip:
    """Values survive a simulated restart."""

    @pytest.mark.asyncio
    async def test_values_survive_restart(self, db: aiosqlite.Connection) -> None:
        store1 = await StatsStore.from_db(db)
        await store1.flush(events_processed=100, actions_taken=50, errors=3)

        # Simulate restart — new StatsStore instance, same database
        store2 = await StatsStore.from_db(db)
        assert store2.values == {"events_processed": 100, "actions_taken": 50, "errors": 3}


# ---------------------------------------------------------------------------
# Background flush loop
# ---------------------------------------------------------------------------


class TestStatsStoreFlushLoop:
    """Start/stop the background flush task."""

    @pytest.mark.asyncio
    async def test_start_creates_task(self, db: aiosqlite.Connection) -> None:
        store = await StatsStore.from_db(db)
        store.start(lambda: (1, 2, 3), interval=60)
        assert store._flush_task is not None
        await store.stop()

    @pytest.mark.asyncio
    async def test_stop_cancels_task(self, db: aiosqlite.Connection) -> None:
        store = await StatsStore.from_db(db)
        store.start(lambda: (1, 2, 3), interval=60)
        await store.stop()
        assert store._flush_task is None

    @pytest.mark.asyncio
    async def test_stop_without_start_is_safe(self, db: aiosqlite.Connection) -> None:
        store = await StatsStore.from_db(db)
        await store.stop()  # Should not raise
