"""T0 — Known fixes registry and match engine.

Loads YAML fix definitions from a directory, validates them with Pydantic,
and provides a deterministic first-match-wins lookup against incoming Events.
No LLM involved — this is the fast path (<1ms).

ARCHITECTURE.md §5 describes the T0 tier and match semantics.
"""

from __future__ import annotations

import json
import logging
from enum import StrEnum
from fnmatch import fnmatch
from pathlib import Path  # noqa: TC003 — used at runtime by Pydantic and load()
from typing import TYPE_CHECKING, Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from oasisagent.models import RiskTier  # noqa: TC001 — used at runtime by Pydantic

if TYPE_CHECKING:
    from oasisagent.models import Event

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class FixActionType(StrEnum):
    """What the known fix prescribes — recommend to a human or auto-fix."""

    RECOMMEND = "recommend"
    AUTO_FIX = "auto_fix"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class FixMatch(BaseModel):
    """Conditions that must all be true for a fix to match an event.

    All fields are optional — you only specify what you need. But at least
    one field must be set (an empty match block is rejected at load time).
    """

    model_config = ConfigDict(extra="forbid")

    system: str | None = None
    event_type: str | None = None
    entity_id_pattern: str | None = None
    payload_contains: str | None = None
    min_duration: int | None = None

    @model_validator(mode="after")
    def _require_at_least_one_condition(self) -> FixMatch:
        has_condition = any([
            self.system is not None,
            self.event_type is not None,
            self.entity_id_pattern is not None,
            self.payload_contains is not None,
            self.min_duration is not None,
        ])
        if not has_condition:
            msg = "FixMatch must have at least one condition — empty match blocks are not allowed"
            raise ValueError(msg)
        return self


class FixAction(BaseModel):
    """What to do when a known fix matches."""

    model_config = ConfigDict(extra="forbid")

    type: FixActionType
    handler: str
    operation: str
    details: dict[str, Any] = Field(default_factory=dict)


class KnownFix(BaseModel):
    """A single known failure → fix mapping."""

    model_config = ConfigDict(extra="forbid")

    id: str
    match: FixMatch
    diagnosis: str
    action: FixAction
    risk_tier: RiskTier


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class KnownFixRegistry:
    """Loads and queries a directory of YAML known-fix definitions.

    Fix files are loaded once at startup. Each file contains a ``fixes``
    list of KnownFix entries. Fixes are evaluated in file-alphabetical
    order, then definition order within each file. First match wins.

    Invalid files are logged and skipped — a bad YAML file should not
    prevent the agent from starting.
    """

    def __init__(self) -> None:
        self._fixes: list[KnownFix] = []

    @property
    def fixes(self) -> list[KnownFix]:
        """All loaded fixes in evaluation order."""
        return list(self._fixes)

    def load(self, directory: Path) -> None:
        """Load all .yaml files from the given directory.

        Files are loaded in sorted order for deterministic evaluation.
        Duplicate fix IDs across files are rejected.
        """
        if not directory.is_dir():
            logger.warning(
                "Known fixes directory does not exist: %s — no T0 patterns loaded",
                directory,
            )
            return

        seen_ids: dict[str, str] = {}  # fix_id -> filename
        fixes: list[KnownFix] = []

        for path in sorted(directory.glob("*.yaml")):
            file_fixes = self._load_file(path)
            for fix in file_fixes:
                if fix.id in seen_ids:
                    logger.error(
                        "Duplicate fix ID '%s' in %s (first seen in %s) — skipping",
                        fix.id,
                        path.name,
                        seen_ids[fix.id],
                    )
                    continue
                seen_ids[fix.id] = path.name
                fixes.append(fix)

        self._fixes = fixes
        logger.info("Loaded %d known fixes from %s", len(self._fixes), directory)

    def get_fix_by_id(self, fix_id: str) -> KnownFix | None:
        """Look up a fix by its unique ID. Returns None if not found."""
        for fix in self._fixes:
            if fix.id == fix_id:
                return fix
        return None

    def match(self, event: Event) -> KnownFix | None:
        """Find the first fix that matches the given event, or None."""
        for fix in self._fixes:
            if self._matches(fix.match, event, fix_id=fix.id):
                return fix
        return None

    @staticmethod
    def _matches(match: FixMatch, event: Event, *, fix_id: str = "") -> bool:
        """Evaluate all conditions in a FixMatch against an Event.

        All specified conditions must be true (logical AND).
        """
        if match.system is not None and match.system != event.system:
            return False

        if match.event_type is not None and match.event_type != event.event_type:
            return False

        if match.entity_id_pattern is not None and not fnmatch(
            event.entity_id, match.entity_id_pattern
        ):
            return False

        if match.payload_contains is not None:
            # Serialize payload as JSON so YAML authors can reason about
            # the exact string they're matching (double quotes, key order).
            payload_json = json.dumps(event.payload, default=str)
            if match.payload_contains not in payload_json:
                return False

        if match.min_duration is not None:
            logger.debug(
                "min_duration=%d is configured for fix '%s' but duration tracking "
                "is not yet implemented. Condition is treated as met.",
                match.min_duration,
                fix_id,
            )

        return True

    @staticmethod
    def _load_file(path: Path) -> list[KnownFix]:
        """Load and validate fixes from a single YAML file."""
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.error("Failed to read known fixes file %s: %s", path.name, exc)
            return []

        try:
            data = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            logger.error("Invalid YAML in known fixes file %s: %s", path.name, exc)
            return []

        if not isinstance(data, dict):
            logger.error(
                "Known fixes file %s: expected a YAML mapping, got %s",
                path.name,
                type(data).__name__,
            )
            return []

        raw_fixes = data.get("fixes", [])
        if not isinstance(raw_fixes, list):
            logger.error(
                "Known fixes file %s: 'fixes' must be a list, got %s",
                path.name,
                type(raw_fixes).__name__,
            )
            return []

        fixes: list[KnownFix] = []
        for i, entry in enumerate(raw_fixes):
            try:
                fixes.append(KnownFix(**entry))
            except Exception as exc:
                logger.error(
                    "Known fixes file %s: fix #%d failed validation: %s",
                    path.name,
                    i + 1,
                    exc,
                )

        return fixes
