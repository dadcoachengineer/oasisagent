"""Tests for InteractiveNotificationChannel ABC and dispatcher integration."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import AsyncMock

import pytest

from oasisagent.approval.pending import (
    ApprovalDecision,
    PendingAction,
    PendingStatus,
)
from oasisagent.models import Notification, RecommendedAction, Severity
from oasisagent.notifications.base import NotificationChannel
from oasisagent.notifications.dispatcher import NotificationDispatcher
from oasisagent.notifications.interactive import InteractiveNotificationChannel

# Type alias to keep callback signatures under line-length limit
_ApprovalCallback = Callable[[str, ApprovalDecision], Awaitable[None]]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_notification(**overrides: Any) -> Notification:
    defaults: dict[str, Any] = {
        "title": "Test Alert",
        "message": "Something happened",
        "severity": Severity.WARNING,
        "event_id": "evt-001",
    }
    defaults.update(overrides)
    return Notification(**defaults)


def _make_pending(**overrides: Any) -> PendingAction:
    from datetime import UTC, datetime, timedelta

    defaults: dict[str, Any] = {
        "event_id": "evt-001",
        "action": RecommendedAction(
            handler="homeassistant",
            operation="restart_integration",
            params={"integration": "zwave"},
            description="Restart Z-Wave integration",
            risk_tier="recommend",
        ),
        "diagnosis": "Z-Wave integration is unresponsive",
        "expires_at": datetime.now(UTC) + timedelta(minutes=30),
    }
    defaults.update(overrides)
    return PendingAction(**defaults)


class _StubChannel(NotificationChannel):
    """Basic (non-interactive) channel for testing mixed dispatch."""

    def __init__(self, channel_name: str = "stub") -> None:
        self._name = channel_name
        self.sent: list[Notification] = []

    async def send(self, notification: Notification) -> bool:
        self.sent.append(notification)
        return True

    def name(self) -> str:
        return self._name


class _StubInteractiveChannel(InteractiveNotificationChannel):
    """Concrete interactive channel for testing."""

    def __init__(self, channel_name: str = "interactive-stub") -> None:
        self._name = channel_name
        self.sent_notifications: list[Notification] = []
        self.sent_approvals: list[PendingAction] = []
        self.updated_messages: list[tuple[str, PendingStatus]] = []
        self.listener_started = False
        self.listener_stopped = False
        self._callback: AsyncMock | None = None

    async def send(self, notification: Notification) -> bool:
        self.sent_notifications.append(notification)
        return True

    def name(self) -> str:
        return self._name

    async def send_approval_request(self, pending: PendingAction) -> None:
        self.sent_approvals.append(pending)

    async def start_listener(
        self,
        callback: _ApprovalCallback,
    ) -> None:
        self.listener_started = True
        self._callback = callback

    async def stop_listener(self) -> None:
        self.listener_stopped = True

    async def update_approval_message(
        self, action_id: str, status: PendingStatus,
    ) -> None:
        self.updated_messages.append((action_id, status))


class _FailingInteractiveChannel(_StubInteractiveChannel):
    """Interactive channel that raises on every operation."""

    def __init__(self) -> None:
        super().__init__("failing-interactive")

    async def send_approval_request(self, pending: PendingAction) -> None:
        msg = "send_approval_request failed"
        raise RuntimeError(msg)

    async def update_approval_message(
        self, action_id: str, status: PendingStatus,
    ) -> None:
        msg = "update_approval_message failed"
        raise RuntimeError(msg)


# ---------------------------------------------------------------------------
# ABC contract tests
# ---------------------------------------------------------------------------


class TestInteractiveNotificationChannelABC:
    """Test the ABC itself — contract enforcement and inheritance."""

    def test_cannot_instantiate_abc(self) -> None:
        with pytest.raises(TypeError, match="abstract"):
            InteractiveNotificationChannel()  # type: ignore[abstract]

    def test_extends_notification_channel(self) -> None:
        assert issubclass(InteractiveNotificationChannel, NotificationChannel)

    def test_concrete_implementation_works(self) -> None:
        channel = _StubInteractiveChannel()
        assert isinstance(channel, InteractiveNotificationChannel)
        assert isinstance(channel, NotificationChannel)

    def test_must_implement_send(self) -> None:
        """A subclass missing send() cannot be instantiated."""

        class _MissingSend(InteractiveNotificationChannel):
            def name(self) -> str:
                return "missing-send"

            async def send_approval_request(self, pending: PendingAction) -> None:
                pass

            async def start_listener(self, callback: _ApprovalCallback) -> None:
                pass

            async def stop_listener(self) -> None:
                pass

        with pytest.raises(TypeError, match="abstract"):
            _MissingSend()  # type: ignore[abstract]

    def test_must_implement_send_approval_request(self) -> None:
        class _MissingApproval(InteractiveNotificationChannel):
            async def send(self, notification: Notification) -> bool:
                return True

            def name(self) -> str:
                return "missing-approval"

            async def start_listener(self, callback: _ApprovalCallback) -> None:
                pass

            async def stop_listener(self) -> None:
                pass

        with pytest.raises(TypeError, match="abstract"):
            _MissingApproval()  # type: ignore[abstract]

    def test_must_implement_start_listener(self) -> None:
        class _MissingStartListener(InteractiveNotificationChannel):
            async def send(self, notification: Notification) -> bool:
                return True

            def name(self) -> str:
                return "missing-start"

            async def send_approval_request(self, pending: PendingAction) -> None:
                pass

            async def stop_listener(self) -> None:
                pass

        with pytest.raises(TypeError, match="abstract"):
            _MissingStartListener()  # type: ignore[abstract]

    def test_must_implement_stop_listener(self) -> None:
        class _MissingStopListener(InteractiveNotificationChannel):
            async def send(self, notification: Notification) -> bool:
                return True

            def name(self) -> str:
                return "missing-stop"

            async def send_approval_request(self, pending: PendingAction) -> None:
                pass

            async def start_listener(self, callback: _ApprovalCallback) -> None:
                pass

        with pytest.raises(TypeError, match="abstract"):
            _MissingStopListener()  # type: ignore[abstract]

    async def test_update_approval_message_default_noop(self) -> None:
        """Default update_approval_message is a no-op (non-abstract)."""
        channel = _StubInteractiveChannel()
        # Override to use base class default
        await InteractiveNotificationChannel.update_approval_message(
            channel, "action-1", PendingStatus.APPROVED,
        )
        # Should not raise, no side effects


# ---------------------------------------------------------------------------
# ApprovalDecision enum tests
# ---------------------------------------------------------------------------


class TestApprovalDecision:
    def test_is_str_enum(self) -> None:
        assert isinstance(ApprovalDecision.APPROVED, str)
        assert isinstance(ApprovalDecision.REJECTED, str)

    def test_values(self) -> None:
        assert ApprovalDecision.APPROVED == "approved"
        assert ApprovalDecision.REJECTED == "rejected"

    def test_serializes_as_string(self) -> None:
        assert str(ApprovalDecision.APPROVED) == "approved"
        assert f"Decision: {ApprovalDecision.REJECTED}" == "Decision: rejected"


# ---------------------------------------------------------------------------
# Dispatcher interactive dispatch tests
# ---------------------------------------------------------------------------


class TestDispatcherInteractiveChannels:
    """Test dispatcher fan-out for interactive channels."""

    def test_interactive_channels_property_filters_correctly(self) -> None:
        basic = _StubChannel("basic")
        interactive = _StubInteractiveChannel("interactive")
        dispatcher = NotificationDispatcher([basic, interactive])

        assert len(dispatcher.interactive_channels) == 1
        assert dispatcher.interactive_channels[0] is interactive

    def test_interactive_channels_empty_when_none_registered(self) -> None:
        basic = _StubChannel("basic")
        dispatcher = NotificationDispatcher([basic])

        assert dispatcher.interactive_channels == []

    async def test_dispatch_still_works_for_all_channels(self) -> None:
        """Regular dispatch sends to both basic and interactive channels."""
        basic = _StubChannel("basic")
        interactive = _StubInteractiveChannel("interactive")
        dispatcher = NotificationDispatcher([basic, interactive])

        notification = _make_notification()
        results = await dispatcher.dispatch(notification)

        assert results == {"basic": True, "interactive": True}
        assert len(basic.sent) == 1
        assert len(interactive.sent_notifications) == 1

    async def test_dispatch_approval_request_to_interactive_only(self) -> None:
        """Approval requests go to interactive channels only."""
        basic = _StubChannel("basic")
        interactive = _StubInteractiveChannel("interactive")
        dispatcher = NotificationDispatcher([basic, interactive])

        pending = _make_pending()
        results = await dispatcher.dispatch_approval_request(pending)

        assert results == {"interactive": True}
        assert len(interactive.sent_approvals) == 1
        assert interactive.sent_approvals[0].id == pending.id
        # Basic channel should not receive approval requests
        assert len(basic.sent) == 0

    async def test_dispatch_approval_request_multiple_interactive(self) -> None:
        """Approval requests fan out to all interactive channels."""
        ch1 = _StubInteractiveChannel("telegram")
        ch2 = _StubInteractiveChannel("slack")
        dispatcher = NotificationDispatcher([ch1, ch2])

        pending = _make_pending()
        results = await dispatcher.dispatch_approval_request(pending)

        assert results == {"telegram": True, "slack": True}
        assert len(ch1.sent_approvals) == 1
        assert len(ch2.sent_approvals) == 1

    async def test_dispatch_approval_request_failure_isolated(self) -> None:
        """One interactive channel failing doesn't affect others."""
        good = _StubInteractiveChannel("good")
        bad = _FailingInteractiveChannel()
        dispatcher = NotificationDispatcher([good, bad])

        pending = _make_pending()
        results = await dispatcher.dispatch_approval_request(pending)

        assert results == {"good": True, "failing-interactive": False}
        assert len(good.sent_approvals) == 1

    async def test_dispatch_approval_request_no_interactive_channels(self) -> None:
        """No-op when no interactive channels are registered."""
        basic = _StubChannel("basic")
        dispatcher = NotificationDispatcher([basic])

        pending = _make_pending()
        results = await dispatcher.dispatch_approval_request(pending)

        assert results == {}

    async def test_update_approval_messages_fans_out(self) -> None:
        """update_approval_messages notifies all interactive channels."""
        ch1 = _StubInteractiveChannel("telegram")
        ch2 = _StubInteractiveChannel("slack")
        basic = _StubChannel("basic")
        dispatcher = NotificationDispatcher([basic, ch1, ch2])

        await dispatcher.update_approval_messages("action-1", PendingStatus.APPROVED)

        assert ch1.updated_messages == [("action-1", PendingStatus.APPROVED)]
        assert ch2.updated_messages == [("action-1", PendingStatus.APPROVED)]

    async def test_update_approval_messages_failure_isolated(self) -> None:
        """One channel failing to update doesn't affect others."""
        good = _StubInteractiveChannel("good")
        bad = _FailingInteractiveChannel()
        dispatcher = NotificationDispatcher([good, bad])

        # Should not raise
        await dispatcher.update_approval_messages("action-1", PendingStatus.REJECTED)

        assert good.updated_messages == [("action-1", PendingStatus.REJECTED)]

    async def test_update_approval_messages_expired_status(self) -> None:
        """Expired actions also trigger message updates."""
        ch = _StubInteractiveChannel("telegram")
        dispatcher = NotificationDispatcher([ch])

        await dispatcher.update_approval_messages("action-1", PendingStatus.EXPIRED)

        assert ch.updated_messages == [("action-1", PendingStatus.EXPIRED)]


# ---------------------------------------------------------------------------
# Interactive channel lifecycle tests
# ---------------------------------------------------------------------------


class TestInteractiveChannelLifecycle:
    """Test that interactive channels properly track listener state."""

    async def test_start_listener_sets_callback(self) -> None:
        channel = _StubInteractiveChannel()
        callback = AsyncMock()

        await channel.start_listener(callback)

        assert channel.listener_started is True
        assert channel._callback is callback

    async def test_stop_listener(self) -> None:
        channel = _StubInteractiveChannel()

        await channel.stop_listener()

        assert channel.listener_stopped is True

    async def test_callback_invocation(self) -> None:
        """Simulate a channel calling back with an approval decision."""
        channel = _StubInteractiveChannel()
        callback = AsyncMock()
        await channel.start_listener(callback)

        # Simulate what a real channel would do when a button is pressed
        await channel._callback("action-123", ApprovalDecision.APPROVED)

        callback.assert_awaited_once_with("action-123", ApprovalDecision.APPROVED)

    async def test_callback_with_rejection(self) -> None:
        channel = _StubInteractiveChannel()
        callback = AsyncMock()
        await channel.start_listener(callback)

        await channel._callback("action-456", ApprovalDecision.REJECTED)

        callback.assert_awaited_once_with("action-456", ApprovalDecision.REJECTED)
