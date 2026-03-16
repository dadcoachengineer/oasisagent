"""Integration tests for the learning loop — T2 → candidate fix pipeline."""

from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest

from oasisagent.engine.decision import (
    DecisionDisposition,
    DecisionResult,
    DecisionTier,
)
from oasisagent.engine.learning import CandidateFixWriter


def _t2_result_with_suggestion(
    confidence: float = 0.9,
) -> DecisionResult:
    """Build a T2 DecisionResult with a suggested_known_fix."""
    return DecisionResult(
        event_id="evt-123",
        tier=DecisionTier.T2,
        disposition=DecisionDisposition.MATCHED,
        diagnosis="ZWave integration crash",
        details={
            "confidence": confidence,
            "suggested_known_fix": {
                "match": {
                    "system": "homeassistant",
                    "event_type": "integration_failure",
                },
                "diagnosis": "ZWave crash after firmware update",
                "action": {
                    "handler": "homeassistant",
                    "operation": "restart_integration",
                    "params": {"integration": "zwave_js"},
                },
            },
        },
    )


def _t2_result_without_suggestion() -> DecisionResult:
    """Build a T2 DecisionResult without a suggested_known_fix."""
    return DecisionResult(
        event_id="evt-456",
        tier=DecisionTier.T2,
        disposition=DecisionDisposition.MATCHED,
        diagnosis="Unknown failure",
        details={"confidence": 0.3},
    )


# ---------------------------------------------------------------------------
# Decision engine passes through suggested_known_fix
# ---------------------------------------------------------------------------


class TestDecisionDetails:
    def test_result_carries_suggested_fix(self) -> None:
        result = _t2_result_with_suggestion()
        assert "suggested_known_fix" in result.details
        assert result.details["suggested_known_fix"]["match"]["system"] == "homeassistant"

    def test_result_without_fix(self) -> None:
        result = _t2_result_without_suggestion()
        assert "suggested_known_fix" not in result.details


# ---------------------------------------------------------------------------
# End-to-end: T2 → candidate writer
# ---------------------------------------------------------------------------


class TestEndToEnd:
    @pytest.mark.asyncio
    async def test_candidate_written_on_high_confidence(self, tmp_path: Path) -> None:
        async with aiosqlite.connect(":memory:") as db:
            # Create the candidate_fixes table
            await db.execute("""
                CREATE TABLE candidate_fixes (
                    match_hash TEXT PRIMARY KEY,
                    system TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    entity_pattern TEXT NOT NULL DEFAULT '',
                    candidate_yaml TEXT NOT NULL,
                    candidate_path TEXT NOT NULL DEFAULT '',
                    confidence REAL NOT NULL,
                    verified_count INTEGER NOT NULL DEFAULT 1,
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    notified INTEGER NOT NULL DEFAULT 0
                )
            """)

            candidates_dir = tmp_path / "candidates"
            writer = CandidateFixWriter(db, candidates_dir, min_confidence=0.7)

            result = _t2_result_with_suggestion(confidence=0.9)
            suggested = result.details["suggested_known_fix"]
            confidence = result.details["confidence"]

            written = await writer.write_candidate(suggested, confidence)

            assert written is True
            yaml_files = list(candidates_dir.glob("*.yaml"))
            assert len(yaml_files) == 1

            # Verify DB row
            cursor = await db.execute("SELECT * FROM candidate_fixes")
            rows = await cursor.fetchall()
            assert len(rows) == 1

    @pytest.mark.asyncio
    async def test_candidate_not_written_below_threshold(self, tmp_path: Path) -> None:
        async with aiosqlite.connect(":memory:") as db:
            await db.execute("""
                CREATE TABLE candidate_fixes (
                    match_hash TEXT PRIMARY KEY,
                    system TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    entity_pattern TEXT NOT NULL DEFAULT '',
                    candidate_yaml TEXT NOT NULL,
                    candidate_path TEXT NOT NULL DEFAULT '',
                    confidence REAL NOT NULL,
                    verified_count INTEGER NOT NULL DEFAULT 1,
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    notified INTEGER NOT NULL DEFAULT 0
                )
            """)

            candidates_dir = tmp_path / "candidates"
            writer = CandidateFixWriter(db, candidates_dir, min_confidence=0.8)

            result = _t2_result_with_suggestion(confidence=0.5)
            suggested = result.details["suggested_known_fix"]

            written = await writer.write_candidate(suggested, 0.5)

            assert written is False
            assert not candidates_dir.exists() or not list(candidates_dir.glob("*.yaml"))

    @pytest.mark.asyncio
    async def test_duplicate_bumps_count(self, tmp_path: Path) -> None:
        async with aiosqlite.connect(":memory:") as db:
            await db.execute("""
                CREATE TABLE candidate_fixes (
                    match_hash TEXT PRIMARY KEY,
                    system TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    entity_pattern TEXT NOT NULL DEFAULT '',
                    candidate_yaml TEXT NOT NULL,
                    candidate_path TEXT NOT NULL DEFAULT '',
                    confidence REAL NOT NULL,
                    verified_count INTEGER NOT NULL DEFAULT 1,
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    notified INTEGER NOT NULL DEFAULT 0
                )
            """)

            candidates_dir = tmp_path / "candidates"
            writer = CandidateFixWriter(db, candidates_dir, min_confidence=0.7)

            result = _t2_result_with_suggestion(confidence=0.9)
            suggested = result.details["suggested_known_fix"]

            await writer.write_candidate(suggested, 0.9)
            await writer.write_candidate(suggested, 0.9)
            await writer.write_candidate(suggested, 0.9)

            cursor = await db.execute(
                "SELECT verified_count FROM candidate_fixes"
            )
            row = await cursor.fetchone()
            assert row[0] == 3

    @pytest.mark.asyncio
    async def test_ready_after_threshold(self, tmp_path: Path) -> None:
        async with aiosqlite.connect(":memory:") as db:
            await db.execute("""
                CREATE TABLE candidate_fixes (
                    match_hash TEXT PRIMARY KEY,
                    system TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    entity_pattern TEXT NOT NULL DEFAULT '',
                    candidate_yaml TEXT NOT NULL,
                    candidate_path TEXT NOT NULL DEFAULT '',
                    confidence REAL NOT NULL,
                    verified_count INTEGER NOT NULL DEFAULT 1,
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    notified INTEGER NOT NULL DEFAULT 0
                )
            """)

            candidates_dir = tmp_path / "candidates"
            writer = CandidateFixWriter(db, candidates_dir, min_confidence=0.7)

            result = _t2_result_with_suggestion(confidence=0.9)
            suggested = result.details["suggested_known_fix"]

            # Write 3 times to reach threshold
            for _ in range(3):
                await writer.write_candidate(suggested, 0.9)

            ready = await writer.get_ready_candidates(min_verified_count=3)
            assert len(ready) == 1
            assert ready[0]["system"] == "homeassistant"

            # Mark notified
            await writer.mark_notified(ready[0]["match_hash"])

            # Should no longer be ready
            ready2 = await writer.get_ready_candidates(min_verified_count=3)
            assert len(ready2) == 0

    @pytest.mark.asyncio
    async def test_higher_confidence_rewrites_yaml_file(self, tmp_path: Path) -> None:
        """S1 regression: YAML file on disk must be rewritten on confidence update."""
        async with aiosqlite.connect(":memory:") as db:
            await db.execute("""
                CREATE TABLE candidate_fixes (
                    match_hash TEXT PRIMARY KEY,
                    system TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    entity_pattern TEXT NOT NULL DEFAULT '',
                    candidate_yaml TEXT NOT NULL,
                    candidate_path TEXT NOT NULL DEFAULT '',
                    confidence REAL NOT NULL,
                    verified_count INTEGER NOT NULL DEFAULT 1,
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    notified INTEGER NOT NULL DEFAULT 0
                )
            """)

            candidates_dir = tmp_path / "candidates"
            writer = CandidateFixWriter(db, candidates_dir, min_confidence=0.7)

            # First write at 0.8 confidence
            suggested_v1 = _t2_result_with_suggestion(confidence=0.8).details["suggested_known_fix"]
            await writer.write_candidate(suggested_v1, 0.8)

            yaml_files = list(candidates_dir.glob("*.yaml"))
            assert len(yaml_files) == 1
            original_content = yaml_files[0].read_text()

            # Second write with different diagnosis at 0.95 confidence
            result_v2 = _t2_result_with_suggestion(confidence=0.95)
            suggested_v2 = result_v2.details["suggested_known_fix"]
            suggested_v2["diagnosis"] = "Updated diagnosis with more detail"
            await writer.write_candidate(suggested_v2, 0.95)

            # File on disk should be updated (not stale)
            updated_content = yaml_files[0].read_text()
            assert "Updated diagnosis with more detail" in updated_content
            assert updated_content != original_content
