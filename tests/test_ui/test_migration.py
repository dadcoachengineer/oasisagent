"""Tests for migration 002 — user roles."""

from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest

from oasisagent.db.schema import run_migrations


@pytest.fixture
async def migrated_db(tmp_path: Path) -> aiosqlite.Connection:
    """Create a DB with all migrations applied."""
    db = await run_migrations(tmp_path / "test.db")
    yield db
    await db.close()


class TestMigration002:
    async def test_users_table_has_role_column(self, migrated_db: aiosqlite.Connection) -> None:
        """After migration 002, users table should have 'role' column."""
        cursor = await migrated_db.execute("PRAGMA table_info(users)")
        columns = {row["name"] for row in await cursor.fetchall()}
        assert "role" in columns
        assert "is_admin" not in columns

    async def test_users_table_has_auth_columns(self, migrated_db: aiosqlite.Connection) -> None:
        """After migration 002, users table should have all auth columns."""
        cursor = await migrated_db.execute("PRAGMA table_info(users)")
        columns = {row["name"] for row in await cursor.fetchall()}
        assert "jwt_generation" in columns
        assert "totp_confirmed" in columns
        assert "backup_codes_hash" in columns
        assert "failed_attempts" in columns
        assert "locked_until" in columns

    async def test_existing_admin_migrated(self, tmp_path: Path) -> None:
        """An existing is_admin=1 user should become role='admin'."""
        db = await aiosqlite.connect(str(tmp_path / "migrate_test.db"))
        db.row_factory = aiosqlite.Row

        # Create old schema (pre-002) manually
        await db.execute("""
            CREATE TABLE _meta (
                schema_version INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        await db.execute("INSERT INTO _meta (schema_version) VALUES (1)")
        await db.execute("""
            CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                is_admin INTEGER NOT NULL DEFAULT 0,
                totp_secret TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        await db.execute(
            "INSERT INTO users (username, password_hash, is_admin) VALUES (?, ?, ?)",
            ("admin", "hash123", 1),
        )
        await db.execute(
            "INSERT INTO users (username, password_hash, is_admin) VALUES (?, ?, ?)",
            ("viewer", "hash456", 0),
        )
        await db.commit()

        # Run migration 002
        from importlib import import_module

        mod = import_module("oasisagent.db.migrations.002_user_roles")
        await mod.migrate(db)
        await db.commit()

        # Verify
        cursor = await db.execute("SELECT username, role FROM users ORDER BY id")
        rows = await cursor.fetchall()
        assert rows[0]["username"] == "admin"
        assert rows[0]["role"] == "admin"
        assert rows[1]["username"] == "viewer"
        assert rows[1]["role"] == "viewer"

        await db.close()
