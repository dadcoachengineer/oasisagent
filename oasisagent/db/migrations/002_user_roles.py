"""Migrate users table: is_admin → role, add auth columns.

Adds:
- role TEXT (admin/operator/viewer) replacing is_admin boolean
- jwt_generation INTEGER for session invalidation
- totp_confirmed INTEGER for two-step TOTP enrollment
- backup_codes_hash TEXT for hashed single-use recovery codes
- failed_attempts INTEGER for login rate limiting
- locked_until TEXT for account lockout timestamp
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import aiosqlite


async def migrate(db: aiosqlite.Connection) -> None:
    """Recreate users table with role-based auth columns."""
    # Create the new table with all auth columns
    await db.execute("""
        CREATE TABLE users_new (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            username            TEXT    NOT NULL UNIQUE,
            password_hash       TEXT    NOT NULL,
            role                TEXT    NOT NULL DEFAULT 'viewer',
            jwt_generation      INTEGER NOT NULL DEFAULT 0,
            totp_secret         TEXT,
            totp_confirmed      INTEGER NOT NULL DEFAULT 0,
            backup_codes_hash   TEXT,
            failed_attempts     INTEGER NOT NULL DEFAULT 0,
            locked_until        TEXT,
            created_at          TEXT    NOT NULL DEFAULT (datetime('now')),
            updated_at          TEXT    NOT NULL DEFAULT (datetime('now'))
        )
    """)

    # Migrate existing rows: is_admin=1 → role='admin', else 'viewer'
    await db.execute("""
        INSERT INTO users_new (
            id, username, password_hash, role, totp_secret, created_at, updated_at
        )
        SELECT
            id,
            username,
            password_hash,
            CASE WHEN is_admin = 1 THEN 'admin' ELSE 'viewer' END,
            totp_secret,
            created_at,
            updated_at
        FROM users
    """)

    await db.execute("DROP TABLE users")
    await db.execute("ALTER TABLE users_new RENAME TO users")
