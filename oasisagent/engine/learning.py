"""Learning loop — candidate known fix writer.

When T2 diagnoses a novel failure with high confidence and includes a
``suggested_known_fix``, this module writes a candidate YAML file and
tracks occurrences in SQLite. After enough verified occurrences, the
operator is notified to promote the candidate to a real known fix.

Dedup key: ``(system, event_type, entity_id_pattern)`` — hashed as the
match key. On collision with same match but different action, keep the
one with higher T2 confidence.

ARCHITECTURE.md ADR-004: file-based only — agent never shells out to git.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import yaml

if TYPE_CHECKING:
    from pathlib import Path

    import aiosqlite

logger = logging.getLogger(__name__)


def _match_hash(system: str, event_type: str, entity_pattern: str = "") -> str:
    """Compute the dedup hash for a candidate fix match."""
    key = f"{system}:{event_type}:{entity_pattern}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _candidate_to_yaml(candidate: dict[str, Any]) -> str:
    """Convert a suggested_known_fix dict to known fix YAML format."""
    match = candidate.get("match", {})
    action = candidate.get("action", {})

    fix = {
        "fixes": [
            {
                "id": (
                    f"candidate-{match.get('system', 'unknown')}"
                    f"-{match.get('event_type', 'unknown')}"
                ),
                "match": {
                    "system": match.get("system", ""),
                    "event_type": match.get("event_type", ""),
                },
                "diagnosis": candidate.get("diagnosis", "T2-suggested fix"),
                "action": {
                    "type": "handle",
                    "handler": action.get("handler", ""),
                    "operation": action.get("operation", ""),
                    "details": action.get("params", action.get("details", {})),
                },
                "risk_tier": "recommend",
            },
        ],
    }
    return yaml.dump(fix, default_flow_style=False, sort_keys=False)


def _validate_candidate(candidate: dict[str, Any]) -> bool:
    """Check that a suggested_known_fix has the minimum required fields."""
    match = candidate.get("match")
    if not isinstance(match, dict):
        return False
    if not match.get("system") or not match.get("event_type"):
        return False
    action = candidate.get("action")
    if not isinstance(action, dict):
        return False
    return bool(action.get("handler") and action.get("operation"))


class CandidateFixWriter:
    """Writes T2-suggested known fixes as candidate YAML files.

    Tracks occurrence counts in SQLite for dedup and notification
    thresholds. YAML is the artifact the operator reviews; SQLite
    tracks operational metadata.
    """

    def __init__(
        self,
        db: aiosqlite.Connection,
        candidates_dir: Path,
        min_confidence: float = 0.8,
    ) -> None:
        self._db = db
        self._candidates_dir = candidates_dir
        self._min_confidence = min_confidence

    async def write_candidate(
        self,
        suggested_fix: dict[str, Any],
        confidence: float,
    ) -> bool:
        """Process a T2-suggested known fix.

        Returns True if the candidate was written or updated, False if
        it was rejected (validation failure, below confidence threshold).
        """
        if confidence < self._min_confidence:
            logger.debug(
                "Candidate fix below confidence threshold (%.2f < %.2f)",
                confidence,
                self._min_confidence,
            )
            return False

        if not _validate_candidate(suggested_fix):
            logger.warning("Invalid candidate fix structure: %s", suggested_fix)
            return False

        match = suggested_fix["match"]
        system = match["system"]
        event_type = match["event_type"]
        entity_pattern = match.get("entity_id_pattern", "")
        mhash = _match_hash(system, event_type, entity_pattern)

        now = datetime.now(tz=UTC).isoformat()

        # Check for existing candidate with same match
        row = await self._db.execute(
            "SELECT match_hash, confidence FROM candidate_fixes WHERE match_hash = ?",
            (mhash,),
        )
        existing = await row.fetchone()

        if existing is not None:
            existing_confidence = existing[1]
            if confidence > existing_confidence:
                # Higher confidence — update the candidate
                candidate_yaml = _candidate_to_yaml(suggested_fix)
                await self._db.execute(
                    """UPDATE candidate_fixes
                       SET candidate_yaml = ?, confidence = ?,
                           verified_count = verified_count + 1,
                           last_seen = ?
                       WHERE match_hash = ?""",
                    (candidate_yaml, confidence, now, mhash),
                )
                logger.info(
                    "Updated candidate fix %s with higher confidence %.2f",
                    mhash,
                    confidence,
                )
            else:
                # Same or lower confidence — just bump count
                await self._db.execute(
                    """UPDATE candidate_fixes
                       SET verified_count = verified_count + 1,
                           last_seen = ?
                       WHERE match_hash = ?""",
                    (now, mhash),
                )
                logger.debug(
                    "Bumped verified_count for candidate %s", mhash,
                )
            await self._db.commit()
            return True

        # New candidate — write YAML and insert row
        candidate_yaml = _candidate_to_yaml(suggested_fix)
        filename = f"candidate-{system}-{event_type}-{mhash}.yaml"
        candidate_path = self._candidates_dir / filename

        self._candidates_dir.mkdir(parents=True, exist_ok=True)
        candidate_path.write_text(candidate_yaml)

        await self._db.execute(
            """INSERT INTO candidate_fixes
               (match_hash, system, event_type, entity_pattern,
                candidate_yaml, candidate_path, confidence,
                verified_count, first_seen, last_seen, notified)
               VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, 0)""",
            (
                mhash, system, event_type, entity_pattern,
                candidate_yaml, str(candidate_path), confidence,
                now, now,
            ),
        )
        await self._db.commit()

        logger.info(
            "New candidate fix written: %s → %s", mhash, candidate_path,
        )
        return True

    async def get_ready_candidates(
        self, min_verified_count: int = 3,
    ) -> list[dict[str, Any]]:
        """Return candidates that have reached the notification threshold.

        Returns candidates where ``verified_count >= min_verified_count``
        and ``notified = 0``.
        """
        cursor = await self._db.execute(
            """SELECT match_hash, system, event_type, candidate_yaml,
                      confidence, verified_count, candidate_path
               FROM candidate_fixes
               WHERE verified_count >= ? AND notified = 0""",
            (min_verified_count,),
        )
        rows = await cursor.fetchall()
        return [
            {
                "match_hash": r[0],
                "system": r[1],
                "event_type": r[2],
                "candidate_yaml": r[3],
                "confidence": r[4],
                "verified_count": r[5],
                "candidate_path": r[6],
            }
            for r in rows
        ]

    async def mark_notified(self, match_hash: str) -> None:
        """Mark a candidate as notified so it isn't surfaced again."""
        await self._db.execute(
            "UPDATE candidate_fixes SET notified = 1 WHERE match_hash = ?",
            (match_hash,),
        )
        await self._db.commit()
