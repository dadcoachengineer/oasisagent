"""Shared fixtures for UI tests."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, PropertyMock

import pytest
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient

from oasisagent.db.config_store import ConfigStore
from oasisagent.db.crypto import CryptoProvider
from oasisagent.db.notification_store import NotificationStore
from oasisagent.db.schema import run_migrations
from oasisagent.ui.auth import (
    create_access_token,
    derive_jwt_key,
    hash_password,
)
from oasisagent.web.app import create_app


def _mock_orchestrator() -> MagicMock:
    """Create a mock orchestrator with the attributes the UI reads."""
    orch = MagicMock()
    orch._events_processed = 42
    orch._actions_taken = 7
    orch._errors = 1
    orch._queue = MagicMock()
    type(orch._queue).size = PropertyMock(return_value=3)
    orch._pending_queue = MagicMock()
    type(orch._pending_queue).pending_count = PropertyMock(return_value=0)
    orch._circuit_breaker = MagicMock()
    type(orch._circuit_breaker).state = PropertyMock(return_value="closed")
    orch._registry = MagicMock()
    orch._registry.fixes = []
    return orch


@pytest.fixture
async def db_and_store(tmp_path: Path) -> tuple[Any, ConfigStore, str, str]:
    """Create a fresh database + config store + keys."""
    key = Fernet.generate_key().decode()
    crypto = CryptoProvider(key)
    db = await run_migrations(tmp_path / "test.db")
    store = ConfigStore(db, crypto)
    jwt_key = derive_jwt_key(key)
    return db, store, key, jwt_key


@pytest.fixture
async def setup_client(db_and_store: tuple[Any, ConfigStore, str, str]) -> AsyncClient:
    """Test client in setup mode (no admin user)."""
    db, store, secret_key, jwt_key = db_and_store

    app = create_app()
    app.state.config_store = store
    app.state.crypto = CryptoProvider(secret_key)
    app.state.db = db
    app.state.start_time = time.monotonic()
    app.state.orchestrator = _mock_orchestrator()
    app.state.jwt_signing_key = jwt_key
    app.state.notification_store = NotificationStore(db)
    app.state.web_notification_channel = None

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c  # type: ignore[misc]

    await db.close()


@pytest.fixture
async def auth_client(
    db_and_store: tuple[Any, ConfigStore, str, str],
) -> AsyncClient:
    """Test client with an authenticated admin user (JWT cookie pre-set)."""
    db, store, secret_key, jwt_key = db_and_store

    # Create admin user
    pw_hash = hash_password("securepass123")
    await store.create_user(
        username="admin",
        password_hash=pw_hash,
        role="admin",
        totp_confirmed=True,
    )

    app = create_app()
    app.state.config_store = store
    app.state.crypto = CryptoProvider(secret_key)
    app.state.db = db
    app.state.start_time = time.monotonic()
    app.state.orchestrator = _mock_orchestrator()
    app.state.jwt_signing_key = jwt_key
    app.state.notification_store = NotificationStore(db)
    app.state.web_notification_channel = None

    # Create JWT token for admin
    jwt_token, csrf_token = create_access_token(
        user_id=1, username="admin", role="admin",
        jwt_generation=0, signing_key=jwt_key,
    )

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        cookies={"oasis_access_token": jwt_token},
        headers={"x-csrf-token": csrf_token},
    ) as c:
        yield c  # type: ignore[misc]

    await db.close()


@pytest.fixture
async def viewer_client(
    db_and_store: tuple[Any, ConfigStore, str, str],
) -> AsyncClient:
    """Test client with an authenticated viewer user."""
    db, store, secret_key, jwt_key = db_and_store

    # Create admin (required for setup guard)
    pw_hash = hash_password("adminpass123")
    await store.create_user(
        username="admin", password_hash=pw_hash, role="admin", totp_confirmed=True,
    )

    # Create viewer
    pw_hash = hash_password("viewerpass123")
    await store.create_user(
        username="viewer", password_hash=pw_hash, role="viewer",
    )

    app = create_app()
    app.state.config_store = store
    app.state.crypto = CryptoProvider(secret_key)
    app.state.db = db
    app.state.start_time = time.monotonic()
    app.state.orchestrator = _mock_orchestrator()
    app.state.jwt_signing_key = jwt_key
    app.state.notification_store = NotificationStore(db)
    app.state.web_notification_channel = None

    jwt_token, csrf_token = create_access_token(
        user_id=2, username="viewer", role="viewer",
        jwt_generation=0, signing_key=jwt_key,
    )

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        cookies={"oasis_access_token": jwt_token},
        headers={"x-csrf-token": csrf_token},
    ) as c:
        yield c  # type: ignore[misc]

    await db.close()
