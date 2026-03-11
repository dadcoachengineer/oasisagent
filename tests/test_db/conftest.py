"""Shared fixtures for SQLite config backend tests."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from oasisagent.db.crypto import CryptoProvider
from oasisagent.db.schema import run_migrations

if TYPE_CHECKING:
    import aiosqlite

    from oasisagent.db.config_store import ConfigStore as ConfigStoreType


@pytest.fixture
def secret_key() -> str:
    """A fixed Fernet key for deterministic tests."""
    from cryptography.fernet import Fernet

    return Fernet.generate_key().decode()


@pytest.fixture
def crypto(secret_key: str) -> CryptoProvider:
    """A CryptoProvider with a test key."""
    return CryptoProvider(secret_key)


@pytest.fixture
async def db(tmp_path: Path) -> aiosqlite.Connection:
    """An in-memory-like SQLite database with migrations applied."""
    db_path = tmp_path / "test.db"
    connection = await run_migrations(db_path)
    yield connection  # type: ignore[misc]
    await connection.close()


@pytest.fixture
async def store(db: aiosqlite.Connection, crypto: CryptoProvider) -> ConfigStoreType:
    """A ConfigStore wired to the test database and crypto provider."""
    from oasisagent.db.config_store import ConfigStore

    return ConfigStore(db, crypto)
