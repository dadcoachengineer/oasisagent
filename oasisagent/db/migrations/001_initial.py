"""Initial schema: agent config, connectors, services, notifications, users."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import aiosqlite


async def migrate(db: aiosqlite.Connection) -> None:
    """Create all tables for the v0.3.0 config backend."""
    # -- Agent config (single-row) ------------------------------------------
    await db.execute("""
        CREATE TABLE agent_config (
            id                      INTEGER PRIMARY KEY CHECK (id = 1),
            name                    TEXT    NOT NULL DEFAULT 'oasis-agent',
            log_level               TEXT    NOT NULL DEFAULT 'info',
            event_queue_size        INTEGER NOT NULL DEFAULT 1000,
            dedup_window_seconds    INTEGER NOT NULL DEFAULT 300,
            shutdown_timeout        INTEGER NOT NULL DEFAULT 30,
            event_ttl               INTEGER NOT NULL DEFAULT 300,
            known_fixes_dir         TEXT    NOT NULL DEFAULT 'known_fixes/',
            correlation_window      INTEGER NOT NULL DEFAULT 30,
            metrics_port            INTEGER NOT NULL DEFAULT 0
        )
    """)
    # Seed the single row with defaults
    await db.execute("INSERT INTO agent_config (id) VALUES (1)")

    # -- Connectors (ingestion adapters) ------------------------------------
    await db.execute("""
        CREATE TABLE connectors (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            type            TEXT    NOT NULL,
            name            TEXT    NOT NULL UNIQUE,
            enabled         INTEGER NOT NULL DEFAULT 1,
            config_json     TEXT    NOT NULL DEFAULT '{}',
            secrets_json    TEXT    NOT NULL DEFAULT '',
            created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
            updated_at      TEXT    NOT NULL DEFAULT (datetime('now'))
        )
    """)

    # -- Core services (handlers, LLM, audit, guardrails) -------------------
    await db.execute("""
        CREATE TABLE core_services (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            type            TEXT    NOT NULL,
            name            TEXT    NOT NULL UNIQUE,
            enabled         INTEGER NOT NULL DEFAULT 1,
            config_json     TEXT    NOT NULL DEFAULT '{}',
            secrets_json    TEXT    NOT NULL DEFAULT '',
            created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
            updated_at      TEXT    NOT NULL DEFAULT (datetime('now'))
        )
    """)

    # -- Notification channels ----------------------------------------------
    await db.execute("""
        CREATE TABLE notification_channels (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            type            TEXT    NOT NULL,
            name            TEXT    NOT NULL UNIQUE,
            enabled         INTEGER NOT NULL DEFAULT 1,
            config_json     TEXT    NOT NULL DEFAULT '{}',
            secrets_json    TEXT    NOT NULL DEFAULT '',
            created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
            updated_at      TEXT    NOT NULL DEFAULT (datetime('now'))
        )
    """)

    # -- Users (for web UI auth, populated by setup wizard #49) -------------
    await db.execute("""
        CREATE TABLE users (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            username        TEXT    NOT NULL UNIQUE,
            password_hash   TEXT    NOT NULL,
            is_admin        INTEGER NOT NULL DEFAULT 0,
            totp_secret     TEXT,
            created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
            updated_at      TEXT    NOT NULL DEFAULT (datetime('now'))
        )
    """)
