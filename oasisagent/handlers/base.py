"""Handler abstract base class.

All handlers implement this interface. The decision engine dispatches
actions to handlers based on the ``handler`` field in RecommendedAction.

ARCHITECTURE.md §8 defines the handler contract.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from oasisagent.models import ActionResult, Event, RecommendedAction, VerifyResult


class Handler(ABC):
    """Base class for all system handlers.

    Handlers are the "hands" — they execute actions against managed
    systems (Home Assistant, Docker, Proxmox, etc.).
    """

    @abstractmethod
    def name(self) -> str:
        """Handler identifier, matches Event.system and RecommendedAction.handler."""

    @abstractmethod
    async def can_handle(self, event: Event, action: RecommendedAction) -> bool:
        """Check if this handler can execute the given action."""

    @abstractmethod
    async def execute(self, event: Event, action: RecommendedAction) -> ActionResult:
        """Execute the action. Returns success/failure with details."""

    @abstractmethod
    async def verify(
        self, event: Event, action: RecommendedAction, result: ActionResult
    ) -> VerifyResult:
        """Verify the action had the desired effect."""

    @abstractmethod
    async def get_context(self, event: Event) -> dict[str, Any]:
        """Gather system-specific context for diagnosis (called by T1/T2)."""

    async def start(self) -> None:  # noqa: B027 — intentional non-abstract default
        """Initialize handler resources (e.g., HTTP sessions).

        Default no-op. Override in handlers that need async setup.
        """

    async def stop(self) -> None:  # noqa: B027 — intentional non-abstract default
        """Clean up handler resources.

        Default no-op. Override in handlers that need async teardown.
        """
