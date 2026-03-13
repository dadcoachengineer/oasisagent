"""Tests for the SQLite migration framework."""

from __future__ import annotations

from pathlib import Path

from oasisagent.db.schema import _discover_migrations, _get_schema_version, run_migrations


class TestMigrationDiscovery:
    def test_discovers_initial_migration(self) -> None:
        migrations = _discover_migrations()
        assert len(migrations) >= 1
        assert migrations[0] == (1, "001_initial")

    def test_migrations_are_sorted(self) -> None:
        migrations = _discover_migrations()
        versions = [v for v, _ in migrations]
        assert versions == sorted(versions)


class TestRunMigrations:
    async def test_fresh_db_runs_all_migrations(self, tmp_path: Path) -> None:
        db = await run_migrations(tmp_path / "test.db")
        try:
            version = await _get_schema_version(db)
            assert version >= 1
        finally:
            await db.close()

    async def test_idempotent_on_second_run(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        db1 = await run_migrations(db_path)
        v1 = await _get_schema_version(db1)
        await db1.close()

        db2 = await run_migrations(db_path)
        v2 = await _get_schema_version(db2)
        await db2.close()

        assert v1 == v2

    async def test_creates_all_tables(self, tmp_path: Path) -> None:
        db = await run_migrations(tmp_path / "test.db")
        try:
            cursor = await db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
            tables = {row[0] for row in await cursor.fetchall()}
            expected = {
                "_meta", "agent_config", "connectors",
                "core_services", "notification_channels", "users",
            }
            assert expected.issubset(tables)
        finally:
            await db.close()

    async def test_agent_config_has_default_row(self, tmp_path: Path) -> None:
        db = await run_migrations(tmp_path / "test.db")
        try:
            cursor = await db.execute("SELECT * FROM agent_config WHERE id = 1")
            row = await cursor.fetchone()
            assert row is not None
            assert row[1] == "oasis-agent"  # name column
        finally:
            await db.close()

    async def test_wal_mode_enabled(self, tmp_path: Path) -> None:
        db = await run_migrations(tmp_path / "test.db")
        try:
            cursor = await db.execute("PRAGMA journal_mode")
            row = await cursor.fetchone()
            assert row[0] == "wal"
        finally:
            await db.close()

    async def test_creates_parent_directories(self, tmp_path: Path) -> None:
        nested_path = tmp_path / "a" / "b" / "c" / "test.db"
        db = await run_migrations(nested_path)
        try:
            assert nested_path.exists()
        finally:
            await db.close()
