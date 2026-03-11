"""Tests for the T0 known fixes registry and match engine."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from oasisagent.engine.known_fixes import (
    FixAction,
    FixActionType,
    FixMatch,
    KnownFix,
    KnownFixRegistry,
)
from oasisagent.models import Event, EventMetadata, RiskTier, Severity

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(**overrides: Any) -> Event:
    defaults: dict[str, Any] = {
        "source": "test",
        "system": "homeassistant",
        "event_type": "automation_error",
        "entity_id": "automation.kitchen_lights",
        "severity": Severity.ERROR,
        "timestamp": datetime.now(UTC),
        "payload": {"error": "kelvin deprecated"},
        "metadata": EventMetadata(),
    }
    defaults.update(overrides)
    return Event(**defaults)


def _make_fix(**overrides: Any) -> KnownFix:
    defaults: dict[str, Any] = {
        "id": "test-fix",
        "match": FixMatch(system="homeassistant"),
        "diagnosis": "Test diagnosis",
        "action": FixAction(
            type=FixActionType.RECOMMEND,
            handler="homeassistant",
            operation="notify",
        ),
        "risk_tier": RiskTier.RECOMMEND,
    }
    defaults.update(overrides)
    return KnownFix(**defaults)


def _write_yaml(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Model validation
# ---------------------------------------------------------------------------


class TestFixActionType:
    """Tests for FixActionType enum."""

    def test_valid_values(self) -> None:
        assert FixActionType.RECOMMEND == "recommend"
        assert FixActionType.AUTO_FIX == "auto_fix"

    def test_invalid_value_rejected(self) -> None:
        with pytest.raises(ValidationError):
            FixAction(type="reccomend", handler="ha", operation="notify")


class TestFixMatch:
    """Tests for FixMatch model validation."""

    def test_empty_match_rejected(self) -> None:
        with pytest.raises(ValueError, match="at least one condition"):
            FixMatch()

    def test_single_field_accepted(self) -> None:
        m = FixMatch(system="homeassistant")
        assert m.system == "homeassistant"

    def test_all_fields_accepted(self) -> None:
        m = FixMatch(
            system="homeassistant",
            event_type="automation_error",
            entity_id_pattern="*.zwave_*",
            payload_contains="kelvin",
            min_duration=300,
        )
        assert m.min_duration == 300

    def test_extra_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            FixMatch(system="ha", bogus="field")


class TestKnownFixModel:
    """Tests for KnownFix model validation."""

    def test_extra_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_fix(bogus="field")

    def test_risk_tier_uses_enum(self) -> None:
        fix = _make_fix(risk_tier="recommend")
        assert fix.risk_tier == RiskTier.RECOMMEND

    def test_invalid_risk_tier_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_fix(risk_tier="low")


# ---------------------------------------------------------------------------
# get_fix_by_id
# ---------------------------------------------------------------------------


class TestGetFixById:
    """Tests for looking up fixes by ID."""

    def test_found(self) -> None:
        registry = KnownFixRegistry()
        fix = _make_fix(id="ha-restart-zwave")
        registry._fixes = [fix]

        result = registry.get_fix_by_id("ha-restart-zwave")
        assert result is fix

    def test_not_found(self) -> None:
        registry = KnownFixRegistry()
        registry._fixes = [_make_fix(id="ha-restart-zwave")]

        result = registry.get_fix_by_id("nonexistent")
        assert result is None

    def test_empty_registry(self) -> None:
        registry = KnownFixRegistry()

        result = registry.get_fix_by_id("anything")
        assert result is None


# ---------------------------------------------------------------------------
# Match engine
# ---------------------------------------------------------------------------


class TestMatchEngine:
    """Tests for the registry's match logic."""

    def test_exact_system_match(self) -> None:
        registry = KnownFixRegistry()
        registry._fixes = [_make_fix(match=FixMatch(system="homeassistant"))]

        result = registry.match(_make_event(system="homeassistant"))
        assert result is not None
        assert result.id == "test-fix"

    def test_exact_system_no_match(self) -> None:
        registry = KnownFixRegistry()
        registry._fixes = [_make_fix(match=FixMatch(system="docker"))]

        assert registry.match(_make_event(system="homeassistant")) is None

    def test_exact_event_type_match(self) -> None:
        registry = KnownFixRegistry()
        registry._fixes = [_make_fix(match=FixMatch(event_type="automation_error"))]

        result = registry.match(_make_event(event_type="automation_error"))
        assert result is not None

    def test_glob_entity_id_match(self) -> None:
        registry = KnownFixRegistry()
        registry._fixes = [
            _make_fix(match=FixMatch(entity_id_pattern="*.zwave_*")),
        ]

        result = registry.match(_make_event(entity_id="sensor.zwave_temp"))
        assert result is not None

    def test_glob_entity_id_no_match(self) -> None:
        registry = KnownFixRegistry()
        registry._fixes = [
            _make_fix(match=FixMatch(entity_id_pattern="*.zwave_*")),
        ]

        assert registry.match(_make_event(entity_id="sensor.wifi_temp")) is None

    def test_payload_contains_match(self) -> None:
        registry = KnownFixRegistry()
        registry._fixes = [
            _make_fix(match=FixMatch(payload_contains="kelvin")),
        ]

        result = registry.match(_make_event(payload={"error": "kelvin deprecated"}))
        assert result is not None

    def test_payload_contains_no_match(self) -> None:
        registry = KnownFixRegistry()
        registry._fixes = [
            _make_fix(match=FixMatch(payload_contains="kelvin")),
        ]

        assert registry.match(_make_event(payload={"error": "timeout"})) is None

    def test_payload_contains_uses_json_serialization(self) -> None:
        """payload_contains matches against json.dumps() output."""
        registry = KnownFixRegistry()
        # JSON uses double quotes, so matching a key requires double-quote syntax
        registry._fixes = [
            _make_fix(match=FixMatch(payload_contains='"error"')),
        ]

        result = registry.match(_make_event(payload={"error": "something"}))
        assert result is not None

    def test_compound_all_must_match(self) -> None:
        registry = KnownFixRegistry()
        registry._fixes = [
            _make_fix(match=FixMatch(
                system="homeassistant",
                event_type="automation_error",
                payload_contains="kelvin",
            )),
        ]

        result = registry.match(_make_event(
            system="homeassistant",
            event_type="automation_error",
            payload={"error": "kelvin deprecated"},
        ))
        assert result is not None

    def test_compound_partial_match_fails(self) -> None:
        registry = KnownFixRegistry()
        registry._fixes = [
            _make_fix(match=FixMatch(
                system="homeassistant",
                event_type="automation_error",
                payload_contains="kelvin",
            )),
        ]

        # System and event_type match, but payload doesn't contain "kelvin"
        assert registry.match(_make_event(
            system="homeassistant",
            event_type="automation_error",
            payload={"error": "timeout"},
        )) is None

    def test_first_match_wins(self) -> None:
        registry = KnownFixRegistry()
        registry._fixes = [
            _make_fix(id="specific", match=FixMatch(
                system="homeassistant",
                payload_contains="kelvin",
            )),
            _make_fix(id="general", match=FixMatch(
                system="homeassistant",
            )),
        ]

        result = registry.match(_make_event(payload={"error": "kelvin deprecated"}))
        assert result is not None
        assert result.id == "specific"

    def test_first_match_wins_falls_through(self) -> None:
        registry = KnownFixRegistry()
        registry._fixes = [
            _make_fix(id="specific", match=FixMatch(
                system="homeassistant",
                payload_contains="kelvin",
            )),
            _make_fix(id="general", match=FixMatch(
                system="homeassistant",
            )),
        ]

        # No "kelvin" in payload — first fix doesn't match, second does
        result = registry.match(_make_event(payload={"error": "timeout"}))
        assert result is not None
        assert result.id == "general"

    def test_no_fixes_returns_none(self) -> None:
        registry = KnownFixRegistry()
        assert registry.match(_make_event()) is None

    def test_min_duration_logs_debug(self, caplog: pytest.LogCaptureFixture) -> None:
        registry = KnownFixRegistry()
        registry._fixes = [
            _make_fix(match=FixMatch(system="homeassistant", min_duration=300)),
        ]

        with caplog.at_level(logging.DEBUG):
            result = registry.match(_make_event())

        # min_duration is treated as met (condition passes)
        assert result is not None
        assert any("min_duration" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# YAML loading
# ---------------------------------------------------------------------------


class TestYamlLoading:
    """Tests for loading fix definitions from YAML files."""

    def test_load_valid_file(self, tmp_path: Path) -> None:
        _write_yaml(tmp_path / "test.yaml", """
fixes:
  - id: test-1
    match:
      system: homeassistant
    diagnosis: "Test diagnosis"
    action:
      type: recommend
      handler: ha
      operation: notify
    risk_tier: recommend
""")
        registry = KnownFixRegistry()
        registry.load(tmp_path)

        assert len(registry.fixes) == 1
        assert registry.fixes[0].id == "test-1"

    def test_load_multiple_files_sorted(self, tmp_path: Path) -> None:
        for name in ["b_docker.yaml", "a_ha.yaml"]:
            _write_yaml(tmp_path / name, f"""
fixes:
  - id: fix-{name}
    match:
      system: test
    diagnosis: "From {name}"
    action:
      type: recommend
      handler: test
      operation: notify
    risk_tier: recommend
""")
        registry = KnownFixRegistry()
        registry.load(tmp_path)

        assert len(registry.fixes) == 2
        # a_ha.yaml sorts before b_docker.yaml
        assert registry.fixes[0].id == "fix-a_ha.yaml"
        assert registry.fixes[1].id == "fix-b_docker.yaml"

    def test_load_empty_directory(self, tmp_path: Path) -> None:
        registry = KnownFixRegistry()
        registry.load(tmp_path)

        assert len(registry.fixes) == 0

    def test_load_nonexistent_directory(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        registry = KnownFixRegistry()
        with caplog.at_level(logging.WARNING):
            registry.load(tmp_path / "nonexistent")

        assert len(registry.fixes) == 0
        assert any("does not exist" in r.message for r in caplog.records)

    def test_load_empty_fixes_list(self, tmp_path: Path) -> None:
        _write_yaml(tmp_path / "empty.yaml", "fixes: []")
        registry = KnownFixRegistry()
        registry.load(tmp_path)

        assert len(registry.fixes) == 0

    def test_invalid_yaml_skipped(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        _write_yaml(tmp_path / "bad.yaml", "fixes:\n  - [invalid yaml{{{")
        registry = KnownFixRegistry()
        with caplog.at_level(logging.ERROR):
            registry.load(tmp_path)

        assert len(registry.fixes) == 0

    def test_validation_error_skipped(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        _write_yaml(tmp_path / "bad.yaml", """
fixes:
  - id: bad-fix
    match: {}
    diagnosis: "Missing conditions"
    action:
      type: recommend
      handler: ha
      operation: notify
    risk_tier: recommend
""")
        registry = KnownFixRegistry()
        with caplog.at_level(logging.ERROR):
            registry.load(tmp_path)

        assert len(registry.fixes) == 0
        assert any("failed validation" in r.message for r in caplog.records)

    def test_non_mapping_yaml_skipped(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        _write_yaml(tmp_path / "bad.yaml", "- just a list")
        registry = KnownFixRegistry()
        with caplog.at_level(logging.ERROR):
            registry.load(tmp_path)

        assert len(registry.fixes) == 0
        assert any("expected a YAML mapping" in r.message for r in caplog.records)

    def test_duplicate_fix_id_across_files(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        fix_yaml = """
fixes:
  - id: duplicate-id
    match:
      system: homeassistant
    diagnosis: "Test"
    action:
      type: recommend
      handler: ha
      operation: notify
    risk_tier: recommend
"""
        _write_yaml(tmp_path / "a_first.yaml", fix_yaml)
        _write_yaml(tmp_path / "b_second.yaml", fix_yaml)

        registry = KnownFixRegistry()
        with caplog.at_level(logging.ERROR):
            registry.load(tmp_path)

        # First occurrence kept, second skipped
        assert len(registry.fixes) == 1
        assert any("Duplicate fix ID" in r.message for r in caplog.records)

    def test_invalid_action_type_rejected(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        _write_yaml(tmp_path / "bad.yaml", """
fixes:
  - id: bad-type
    match:
      system: homeassistant
    diagnosis: "Test"
    action:
      type: reccomend
      handler: ha
      operation: notify
    risk_tier: recommend
""")
        registry = KnownFixRegistry()
        with caplog.at_level(logging.ERROR):
            registry.load(tmp_path)

        assert len(registry.fixes) == 0

    def test_invalid_risk_tier_rejected(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        _write_yaml(tmp_path / "bad.yaml", """
fixes:
  - id: bad-tier
    match:
      system: homeassistant
    diagnosis: "Test"
    action:
      type: recommend
      handler: ha
      operation: notify
    risk_tier: low
""")
        registry = KnownFixRegistry()
        with caplog.at_level(logging.ERROR):
            registry.load(tmp_path)

        assert len(registry.fixes) == 0


# ---------------------------------------------------------------------------
# Integration: load real fix files
# ---------------------------------------------------------------------------


class TestRealFixFiles:
    """Load the actual known_fixes/ directory to verify YAML is valid."""

    def test_homeassistant_yaml_loads(self) -> None:
        registry = KnownFixRegistry()
        registry.load(Path("known_fixes"))

        assert len(registry.fixes) >= 6
        fix_ids = {f.id for f in registry.fixes}
        assert "ha-deprecated-kelvin" in fix_ids
        assert "ha-zwave-coordinator-unavailable" in fix_ids
        assert "ha-zwave-device-unavailable" in fix_ids
        assert "ha-integration-failure-generic" in fix_ids
        assert "ha-automation-error-generic" in fix_ids
        assert "ha-entity-unavailable-generic" in fix_ids

    def test_kelvin_fix_matches_expected_event(self) -> None:
        registry = KnownFixRegistry()
        registry.load(Path("known_fixes"))

        event = _make_event(
            system="homeassistant",
            event_type="deprecation_warning",
            payload={"message": "Got `kelvin` argument which is deprecated"},
        )
        result = registry.match(event)

        assert result is not None
        assert result.id == "ha-deprecated-kelvin"
        assert result.action.type == FixActionType.RECOMMEND
        assert result.risk_tier == RiskTier.RECOMMEND
