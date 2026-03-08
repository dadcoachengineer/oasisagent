"""Tests for the deterministic safety guardrails."""

from __future__ import annotations

from typing import Any

from oasisagent.config import CircuitBreakerConfig, GuardrailsConfig
from oasisagent.engine.guardrails import GuardrailsEngine
from oasisagent.models import RiskTier

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(**overrides: Any) -> GuardrailsConfig:
    defaults: dict[str, Any] = {
        "blocked_domains": ["lock.*", "alarm_control_panel.*", "camera.*", "cover.*"],
        "blocked_entities": [],
        "kill_switch": False,
        "dry_run": False,
        "circuit_breaker": CircuitBreakerConfig(),
    }
    defaults.update(overrides)
    return GuardrailsConfig(**defaults)


def _engine(**overrides: Any) -> GuardrailsEngine:
    return GuardrailsEngine(_make_config(**overrides))


# ---------------------------------------------------------------------------
# Kill switch
# ---------------------------------------------------------------------------


class TestKillSwitch:
    """Kill switch blocks everything regardless of other settings."""

    def test_blocks_auto_fix(self) -> None:
        result = _engine(kill_switch=True).check("light.kitchen", RiskTier.AUTO_FIX)
        assert result.allowed is False
        assert "kill switch" in result.reason.lower()
        assert result.risk_tier == RiskTier.AUTO_FIX

    def test_blocks_recommend(self) -> None:
        result = _engine(kill_switch=True).check("sensor.temp", RiskTier.RECOMMEND)
        assert result.allowed is False

    def test_blocks_even_safe_entity(self) -> None:
        result = _engine(kill_switch=True).check("light.hallway", RiskTier.RECOMMEND)
        assert result.allowed is False


# ---------------------------------------------------------------------------
# Risk tier policy
# ---------------------------------------------------------------------------


class TestRiskTierPolicy:
    """Risk tier checks run before domain/entity checks."""

    def test_auto_fix_allowed(self) -> None:
        result = _engine().check("light.kitchen", RiskTier.AUTO_FIX)
        assert result.allowed is True
        assert result.risk_tier == RiskTier.AUTO_FIX

    def test_recommend_allowed(self) -> None:
        result = _engine().check("light.kitchen", RiskTier.RECOMMEND)
        assert result.allowed is True
        assert result.risk_tier == RiskTier.RECOMMEND

    def test_escalate_blocked(self) -> None:
        result = _engine().check("light.kitchen", RiskTier.ESCALATE)
        assert result.allowed is False
        assert "human review" in result.reason.lower()
        assert result.risk_tier == RiskTier.ESCALATE

    def test_block_blocked(self) -> None:
        result = _engine().check("light.kitchen", RiskTier.BLOCK)
        assert result.allowed is False
        assert result.risk_tier == RiskTier.BLOCK


# ---------------------------------------------------------------------------
# Blocked domains
# ---------------------------------------------------------------------------


class TestBlockedDomains:
    """Blocked domain patterns use glob matching against entity_id."""

    def test_lock_domain_blocked(self) -> None:
        result = _engine().check("lock.front_door", RiskTier.AUTO_FIX)
        assert result.allowed is False
        assert "blocked domain" in result.reason.lower()

    def test_alarm_panel_blocked(self) -> None:
        result = _engine().check("alarm_control_panel.home", RiskTier.AUTO_FIX)
        assert result.allowed is False

    def test_camera_blocked(self) -> None:
        result = _engine().check("camera.backyard", RiskTier.RECOMMEND)
        assert result.allowed is False

    def test_cover_blocked(self) -> None:
        result = _engine().check("cover.garage_door", RiskTier.AUTO_FIX)
        assert result.allowed is False

    def test_light_domain_not_blocked(self) -> None:
        result = _engine().check("light.kitchen", RiskTier.AUTO_FIX)
        assert result.allowed is True

    def test_custom_blocked_domain(self) -> None:
        result = _engine(blocked_domains=["switch.*"]).check(
            "switch.water_heater", RiskTier.AUTO_FIX
        )
        assert result.allowed is False


# ---------------------------------------------------------------------------
# Blocked entities
# ---------------------------------------------------------------------------


class TestBlockedEntities:
    """Blocked entities are exact match against entity_id."""

    def test_blocked_entity_exact_match(self) -> None:
        result = _engine(blocked_entities=["light.critical_server"]).check(
            "light.critical_server", RiskTier.AUTO_FIX
        )
        assert result.allowed is False
        assert "explicitly blocked" in result.reason.lower()

    def test_non_blocked_entity_allowed(self) -> None:
        result = _engine(blocked_entities=["light.critical_server"]).check(
            "light.kitchen", RiskTier.AUTO_FIX
        )
        assert result.allowed is True

    def test_empty_blocked_entities_allows_all(self) -> None:
        result = _engine(blocked_entities=[]).check("light.anything", RiskTier.AUTO_FIX)
        assert result.allowed is True


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------


class TestDryRun:
    """Dry run mode allows actions but flags them."""

    def test_dry_run_allows_with_flag(self) -> None:
        result = _engine(dry_run=True).check("light.kitchen", RiskTier.AUTO_FIX)
        assert result.allowed is True
        assert result.dry_run is True
        assert "dry run" in result.reason.lower()

    def test_dry_run_off_no_flag(self) -> None:
        result = _engine(dry_run=False).check("light.kitchen", RiskTier.AUTO_FIX)
        assert result.allowed is True
        assert result.dry_run is False


# ---------------------------------------------------------------------------
# Precedence / combined scenarios
# ---------------------------------------------------------------------------


class TestPrecedence:
    """Verify check ordering and precedence."""

    def test_kill_switch_overrides_dry_run(self) -> None:
        result = _engine(kill_switch=True, dry_run=True).check(
            "light.kitchen", RiskTier.AUTO_FIX
        )
        assert result.allowed is False
        assert "kill switch" in result.reason.lower()

    def test_blocked_domain_overrides_auto_fix_tier(self) -> None:
        """Blocklist is a hard override regardless of risk tier."""
        result = _engine().check("lock.front_door", RiskTier.AUTO_FIX)
        assert result.allowed is False
        assert "blocked domain" in result.reason.lower()

    def test_risk_tier_checked_before_blocked_domains(self) -> None:
        """Escalate tier is blocked before we even check domain patterns."""
        result = _engine().check("light.kitchen", RiskTier.ESCALATE)
        assert result.allowed is False
        assert "human review" in result.reason.lower()

    def test_blocked_domain_overrides_dry_run(self) -> None:
        """Blocked domain returns blocked, not dry_run."""
        result = _engine(dry_run=True).check("lock.front_door", RiskTier.AUTO_FIX)
        assert result.allowed is False
        assert "blocked domain" in result.reason.lower()

    def test_blocked_entity_overrides_dry_run(self) -> None:
        result = _engine(
            dry_run=True, blocked_entities=["light.critical"]
        ).check("light.critical", RiskTier.AUTO_FIX)
        assert result.allowed is False
        assert "explicitly blocked" in result.reason.lower()


# ---------------------------------------------------------------------------
# GuardrailResult model
# ---------------------------------------------------------------------------


class TestGuardrailResult:
    """Tests for the GuardrailResult model."""

    def test_carries_risk_tier(self) -> None:
        result = _engine().check("light.kitchen", RiskTier.AUTO_FIX)
        assert result.risk_tier == RiskTier.AUTO_FIX

    def test_blocked_carries_risk_tier(self) -> None:
        result = _engine().check("lock.front_door", RiskTier.AUTO_FIX)
        assert result.risk_tier == RiskTier.AUTO_FIX
        assert result.allowed is False
