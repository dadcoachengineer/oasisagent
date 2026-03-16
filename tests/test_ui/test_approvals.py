"""Tests for the approval queue UI routes.

Uses a real PendingQueue with synthetic PendingAction objects
to test the list, approve, and reject flows.
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, PropertyMock

import pytest
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient

from oasisagent.approval.pending import PendingQueue
from oasisagent.db.config_store import ConfigStore
from oasisagent.db.crypto import CryptoProvider
from oasisagent.db.schema import run_migrations
from oasisagent.models import RecommendedAction, RiskTier
from oasisagent.ui.auth import (
    create_access_token,
    derive_jwt_key,
    hash_password,
)
from oasisagent.web.app import create_app


def _mock_orchestrator_with_queue() -> tuple[MagicMock, PendingQueue]:
    """Create a mock orchestrator with a real PendingQueue."""
    queue = PendingQueue()
    orch = MagicMock()
    orch._events_processed = 0
    orch._actions_taken = 0
    orch._errors = 0
    orch._queue = MagicMock()
    type(orch._queue).size = PropertyMock(return_value=0)
    orch._pending_queue = queue
    orch._circuit_breaker = MagicMock()
    type(orch._circuit_breaker).state = PropertyMock(return_value="closed")
    orch._registry = MagicMock()
    orch._registry.fixes = []
    # Mock the async processing methods
    orch._process_approval = AsyncMock()
    orch._process_rejection = AsyncMock()
    return orch, queue


_add_counter = 0


async def _add_test_action(queue: PendingQueue, description: str = "Restart nginx") -> str:
    """Add a test action to the queue and return its ID."""
    global _add_counter
    _add_counter += 1
    action = RecommendedAction(
        description=description,
        handler="ha_handler",
        operation="restart_integration",
        params={"integration": "nginx"},
        risk_tier=RiskTier.RECOMMEND,
        reasoning="Container is unhealthy for >5 minutes",
        target_entity_id=f"integration.nginx_{_add_counter}",
    )
    pending = await queue.add(
        event_id=f"evt-test-{_add_counter:03d}",
        action=action,
        diagnosis="Nginx container health check failing since 14:30",
        timeout_minutes=30,
    )
    return pending.id


@pytest.fixture
async def approval_client(tmp_path: Path) -> AsyncClient:
    """Test client with a real PendingQueue on the orchestrator."""
    key = Fernet.generate_key().decode()
    crypto = CryptoProvider(key)
    db = await run_migrations(tmp_path / "test.db")
    store = ConfigStore(db, crypto)
    jwt_key = derive_jwt_key(key)

    pw_hash = hash_password("adminpass")
    await store.create_user(
        username="admin", password_hash=pw_hash, role="admin",
        totp_confirmed=True,
    )

    orch, queue = _mock_orchestrator_with_queue()

    app = create_app()
    app.state.config_store = store
    app.state.crypto = crypto
    app.state.db = db
    app.state.start_time = time.monotonic()
    app.state.orchestrator = orch
    app.state.jwt_signing_key = jwt_key

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
        # Stash queue on client for test access
        c._test_queue = queue  # type: ignore[attr-defined]
        c._test_orch = orch  # type: ignore[attr-defined]
        yield c  # type: ignore[misc]

    await db.close()


@pytest.fixture
async def viewer_approval_client(tmp_path: Path) -> AsyncClient:
    """Test client with viewer role (should be denied for approve/reject)."""
    key = Fernet.generate_key().decode()
    crypto = CryptoProvider(key)
    db = await run_migrations(tmp_path / "test.db")
    store = ConfigStore(db, crypto)
    jwt_key = derive_jwt_key(key)

    pw_hash = hash_password("adminpass")
    await store.create_user(
        username="admin", password_hash=pw_hash, role="admin",
        totp_confirmed=True,
    )
    pw_hash = hash_password("viewerpass")
    await store.create_user(
        username="viewer", password_hash=pw_hash, role="viewer",
    )

    orch, _ = _mock_orchestrator_with_queue()

    app = create_app()
    app.state.config_store = store
    app.state.crypto = crypto
    app.state.db = db
    app.state.start_time = time.monotonic()
    app.state.orchestrator = orch
    app.state.jwt_signing_key = jwt_key

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


# ---------------------------------------------------------------------------
# List page
# ---------------------------------------------------------------------------


class TestApprovalsListPage:
    async def test_empty_queue(self, approval_client: AsyncClient) -> None:
        resp = await approval_client.get("/ui/approvals")
        assert resp.status_code == 200
        assert "No pending approvals" in resp.text

    async def test_shows_pending_actions(
        self, approval_client: AsyncClient,
    ) -> None:
        queue: PendingQueue = approval_client._test_queue  # type: ignore[attr-defined]
        await _add_test_action(queue, "Restart nginx")
        await _add_test_action(queue, "Clear DNS cache")

        resp = await approval_client.get("/ui/approvals")
        assert resp.status_code == 200
        assert "Restart nginx" in resp.text
        assert "Clear DNS cache" in resp.text
        assert "ha_handler.restart_integration" in resp.text

    async def test_shows_diagnosis(
        self, approval_client: AsyncClient,
    ) -> None:
        queue: PendingQueue = approval_client._test_queue  # type: ignore[attr-defined]
        await _add_test_action(queue)

        resp = await approval_client.get("/ui/approvals")
        assert "health check failing since 14:30" in resp.text

    async def test_shows_approve_reject_buttons(
        self, approval_client: AsyncClient,
    ) -> None:
        queue: PendingQueue = approval_client._test_queue  # type: ignore[attr-defined]
        await _add_test_action(queue)

        resp = await approval_client.get("/ui/approvals")
        assert "Approve" in resp.text
        assert "Reject" in resp.text


# ---------------------------------------------------------------------------
# Approve
# ---------------------------------------------------------------------------


class TestApproveAction:
    async def test_approve_calls_orchestrator(
        self, approval_client: AsyncClient,
    ) -> None:
        queue: PendingQueue = approval_client._test_queue  # type: ignore[attr-defined]
        orch: MagicMock = approval_client._test_orch  # type: ignore[attr-defined]
        action_id = await _add_test_action(queue)

        resp = await approval_client.post(f"/ui/approvals/{action_id}/approve")
        assert resp.status_code == 200
        orch._process_approval.assert_awaited_once_with(action_id)

    async def test_approve_returns_updated_card(
        self, approval_client: AsyncClient,
    ) -> None:
        queue: PendingQueue = approval_client._test_queue  # type: ignore[attr-defined]
        action_id = await _add_test_action(queue)

        resp = await approval_client.post(f"/ui/approvals/{action_id}/approve")
        assert resp.status_code == 200
        # Card should still contain the action info
        assert "Restart nginx" in resp.text

    async def test_approve_not_found(
        self, approval_client: AsyncClient,
    ) -> None:
        resp = await approval_client.post(
            "/ui/approvals/nonexistent-id/approve",
        )
        assert resp.status_code == 404

    async def test_approve_orchestrator_error_returns_409(
        self, approval_client: AsyncClient,
    ) -> None:
        """If orchestrator raises during approval, return 409 with error card."""
        queue: PendingQueue = approval_client._test_queue  # type: ignore[attr-defined]
        orch: MagicMock = approval_client._test_orch  # type: ignore[attr-defined]
        action_id = await _add_test_action(queue)
        orch._process_approval.side_effect = RuntimeError("handler crashed")

        resp = await approval_client.post(f"/ui/approvals/{action_id}/approve")
        assert resp.status_code == 409
        assert "Approval failed" in resp.text

        # Reset side effect for other tests
        orch._process_approval.side_effect = None


# ---------------------------------------------------------------------------
# Reject
# ---------------------------------------------------------------------------


class TestRejectAction:
    async def test_reject_calls_orchestrator(
        self, approval_client: AsyncClient,
    ) -> None:
        queue: PendingQueue = approval_client._test_queue  # type: ignore[attr-defined]
        orch: MagicMock = approval_client._test_orch  # type: ignore[attr-defined]
        action_id = await _add_test_action(queue)

        resp = await approval_client.post(f"/ui/approvals/{action_id}/reject")
        assert resp.status_code == 200
        orch._process_rejection.assert_awaited_once_with(action_id)

    async def test_reject_not_found(
        self, approval_client: AsyncClient,
    ) -> None:
        resp = await approval_client.post(
            "/ui/approvals/nonexistent-id/reject",
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# RBAC
# ---------------------------------------------------------------------------


class TestApprovalRBAC:
    async def test_viewer_cannot_view_approvals(
        self, viewer_approval_client: AsyncClient,
    ) -> None:
        """Viewers cannot access the approvals page (requires operator)."""
        resp = await viewer_approval_client.get("/ui/approvals")
        assert resp.status_code == 403

    async def test_viewer_cannot_approve(
        self, viewer_approval_client: AsyncClient,
    ) -> None:
        resp = await viewer_approval_client.post(
            "/ui/approvals/some-id/approve",
        )
        assert resp.status_code == 403

    async def test_viewer_cannot_reject(
        self, viewer_approval_client: AsyncClient,
    ) -> None:
        resp = await viewer_approval_client.post(
            "/ui/approvals/some-id/reject",
        )
        assert resp.status_code == 403
