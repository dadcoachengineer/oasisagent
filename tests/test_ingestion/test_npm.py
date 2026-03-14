"""Tests for the Nginx Proxy Manager polling ingestion adapter."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from oasisagent.config import NpmAdapterConfig
from oasisagent.ingestion.npm import NpmAdapter
from oasisagent.models import Severity


def _make_config(**overrides: object) -> NpmAdapterConfig:
    defaults: dict[str, object] = {
        "enabled": True,
        "url": "http://npm.local:81",
        "email": "admin@example.com",
        "password": "secret",
        "poll_interval": 300,
        "poll_proxy_hosts": True,
        "poll_certificates": True,
        "poll_dead_hosts": True,
        "cert_warning_days": 14,
        "cert_critical_days": 7,
        "timeout": 10,
    }
    defaults.update(overrides)
    return NpmAdapterConfig(**defaults)  # type: ignore[arg-type]


def _mock_queue() -> MagicMock:
    return MagicMock()


def _make_adapter(
    **overrides: object,
) -> tuple[NpmAdapter, MagicMock]:
    config = _make_config(**overrides)
    queue = _mock_queue()
    with patch("oasisagent.ingestion.npm.NpmClient"):
        adapter = NpmAdapter(config, queue)
    return adapter, queue


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


class TestConfigValidation:
    def test_defaults(self) -> None:
        config = NpmAdapterConfig()
        assert config.enabled is False
        assert config.poll_interval == 300
        assert config.poll_proxy_hosts is True
        assert config.poll_certificates is True
        assert config.poll_dead_hosts is True
        assert config.cert_warning_days == 14
        assert config.cert_critical_days == 7

    def test_extra_fields_forbidden(self) -> None:
        with pytest.raises(ValidationError, match="Extra inputs"):
            _make_config(bogus="nope")


# ---------------------------------------------------------------------------
# Adapter identity & lifecycle
# ---------------------------------------------------------------------------


class TestAdapterLifecycle:
    def test_name(self) -> None:
        adapter, _ = _make_adapter()
        assert adapter.name == "npm"

    @pytest.mark.asyncio
    async def test_healthy_initially_false(self) -> None:
        adapter, _ = _make_adapter()
        assert not await adapter.healthy()

    @pytest.mark.asyncio
    async def test_stop_sets_flag(self) -> None:
        adapter, _ = _make_adapter()
        adapter._client = AsyncMock()
        await adapter.stop()
        assert adapter._stopping is True
        assert all(v is False for v in adapter._endpoint_health.values())

    @pytest.mark.asyncio
    async def test_start_connection_failure(self) -> None:
        adapter, _ = _make_adapter()
        adapter._client.connect = AsyncMock(
            side_effect=ConnectionError("refused"),
        )
        await adapter.start()
        assert not await adapter.healthy()


# ---------------------------------------------------------------------------
# Per-endpoint health tracking
# ---------------------------------------------------------------------------


class TestEndpointHealth:
    def test_endpoint_health_initial_state(self) -> None:
        """All endpoints start False before first poll."""
        adapter, _ = _make_adapter()
        assert adapter._endpoint_health == {
            "proxy_hosts": False,
            "certificates": False,
            "dead_hosts": False,
        }

    @pytest.mark.asyncio
    async def test_healthy_any_endpoint_up(self) -> None:
        adapter, _ = _make_adapter()
        adapter._endpoint_health["proxy_hosts"] = True
        adapter._endpoint_health["certificates"] = False
        assert await adapter.healthy() is True

    @pytest.mark.asyncio
    async def test_healthy_all_endpoints_down(self) -> None:
        adapter, _ = _make_adapter()
        assert await adapter.healthy() is False

    @pytest.mark.asyncio
    async def test_healthy_no_endpoints_enabled(self) -> None:
        adapter, _ = _make_adapter(
            poll_proxy_hosts=False,
            poll_certificates=False,
            poll_dead_hosts=False,
        )
        assert adapter._endpoint_health == {}
        assert await adapter.healthy() is False

    def test_health_detail_returns_per_endpoint_status(self) -> None:
        adapter, _ = _make_adapter()
        adapter._endpoint_health["proxy_hosts"] = True
        adapter._endpoint_health["certificates"] = False
        adapter._endpoint_health["dead_hosts"] = True

        detail = adapter.health_detail()
        assert detail == {
            "proxy_hosts": "connected",
            "certificates": "disconnected",
            "dead_hosts": "connected",
        }

    @pytest.mark.asyncio
    async def test_poll_proxy_hosts_failure_isolates_others(self) -> None:
        """Proxy host failure does not affect certs or dead hosts health."""
        adapter, _ = _make_adapter()

        async def _fail_proxy_hosts() -> None:
            msg = "proxy timeout"
            raise RuntimeError(msg)

        adapter._poll_proxy_hosts = _fail_proxy_hosts  # type: ignore[method-assign]
        adapter._poll_certificates = AsyncMock()  # type: ignore[method-assign]
        adapter._poll_dead_hosts = AsyncMock()  # type: ignore[method-assign]

        adapter._stopping = False

        async def _stop_after_one(*_args: object, **_kwargs: object) -> None:
            adapter._stopping = True

        with patch("asyncio.sleep", new=_stop_after_one):
            await adapter._poll_loop()

        assert adapter._endpoint_health["proxy_hosts"] is False
        assert adapter._endpoint_health["certificates"] is True
        assert adapter._endpoint_health["dead_hosts"] is True

    @pytest.mark.asyncio
    async def test_stop_clears_endpoint_health(self) -> None:
        adapter, _ = _make_adapter()
        adapter._endpoint_health["proxy_hosts"] = True
        adapter._endpoint_health["certificates"] = True
        adapter._endpoint_health["dead_hosts"] = True

        adapter._client = AsyncMock()
        await adapter.stop()

        assert all(v is False for v in adapter._endpoint_health.values())


# ---------------------------------------------------------------------------
# Proxy host polling
# ---------------------------------------------------------------------------


class TestProxyHostPolling:
    @pytest.mark.asyncio
    async def test_host_disabled_event(self) -> None:
        adapter, queue = _make_adapter()
        adapter._host_states[1] = "online"

        adapter._client.get = AsyncMock(return_value=[{
            "id": 1,
            "enabled": 0,
            "domain_names": ["app.shearer.live"],
        }])

        await adapter._poll_proxy_hosts()

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "npm_proxy_host_error"
        assert event.severity == Severity.WARNING
        assert event.entity_id == "app.shearer.live"
        assert event.payload["status"] == "disabled"
        assert event.payload["previous_status"] == "online"

    @pytest.mark.asyncio
    async def test_host_recovery_event(self) -> None:
        adapter, queue = _make_adapter()
        adapter._host_states[1] = "disabled"

        adapter._client.get = AsyncMock(return_value=[{
            "id": 1,
            "enabled": 1,
            "domain_names": ["app.shearer.live"],
        }])

        await adapter._poll_proxy_hosts()

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "npm_proxy_host_recovered"
        assert event.severity == Severity.INFO

    @pytest.mark.asyncio
    async def test_same_status_no_event(self) -> None:
        adapter, queue = _make_adapter()
        adapter._host_states[1] = "online"

        adapter._client.get = AsyncMock(return_value=[{
            "id": 1,
            "enabled": 1,
            "domain_names": ["app.shearer.live"],
        }])

        await adapter._poll_proxy_hosts()
        queue.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_first_poll_online_no_event(self) -> None:
        adapter, queue = _make_adapter()

        adapter._client.get = AsyncMock(return_value=[{
            "id": 1,
            "enabled": 1,
            "domain_names": ["app.shearer.live"],
        }])

        await adapter._poll_proxy_hosts()
        queue.put_nowait.assert_not_called()
        assert adapter._host_states[1] == "online"

    @pytest.mark.asyncio
    async def test_first_poll_disabled_emits(self) -> None:
        adapter, queue = _make_adapter()

        adapter._client.get = AsyncMock(return_value=[{
            "id": 1,
            "enabled": 0,
            "domain_names": ["app.shearer.live"],
        }])

        await adapter._poll_proxy_hosts()

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "npm_proxy_host_error"

    @pytest.mark.asyncio
    async def test_host_dedup_key(self) -> None:
        adapter, queue = _make_adapter()
        adapter._host_states[1] = "online"

        adapter._client.get = AsyncMock(return_value=[{
            "id": 1,
            "enabled": 0,
            "domain_names": ["app.shearer.live"],
        }])

        await adapter._poll_proxy_hosts()

        event = queue.put_nowait.call_args[0][0]
        assert event.metadata.dedup_key == "npm:proxy_host:1:status"

    @pytest.mark.asyncio
    async def test_removed_host_evicted_from_tracker(self) -> None:
        adapter, _queue = _make_adapter()
        adapter._host_states = {1: "online", 2: "online"}

        adapter._client.get = AsyncMock(return_value=[{
            "id": 1,
            "enabled": 1,
            "domain_names": ["app.shearer.live"],
        }])

        await adapter._poll_proxy_hosts()

        assert 1 in adapter._host_states
        assert 2 not in adapter._host_states

    @pytest.mark.asyncio
    async def test_no_domain_names_uses_host_id(self) -> None:
        adapter, queue = _make_adapter()

        adapter._client.get = AsyncMock(return_value=[{
            "id": 42,
            "enabled": 0,
            "domain_names": [],
        }])

        await adapter._poll_proxy_hosts()

        event = queue.put_nowait.call_args[0][0]
        assert event.entity_id == "42"


# ---------------------------------------------------------------------------
# Certificate polling
# ---------------------------------------------------------------------------


class TestCertificatePolling:
    @pytest.mark.asyncio
    async def test_cert_warning_event(self) -> None:
        from datetime import UTC, datetime, timedelta

        adapter, queue = _make_adapter()
        expires = datetime.now(tz=UTC) + timedelta(days=10)

        adapter._client.get = AsyncMock(return_value=[{
            "id": 1,
            "domain_names": ["app.shearer.live"],
            "nice_name": "App Cert",
            "expires_on": expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }])

        await adapter._poll_certificates()

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "npm_certificate_expiring"
        assert event.severity == Severity.WARNING
        assert event.entity_id == "app.shearer.live"
        assert event.payload["level"] == "warning"
        assert event.payload["nice_name"] == "App Cert"

    @pytest.mark.asyncio
    async def test_cert_critical_event(self) -> None:
        from datetime import UTC, datetime, timedelta

        adapter, queue = _make_adapter()
        expires = datetime.now(tz=UTC) + timedelta(days=3)

        adapter._client.get = AsyncMock(return_value=[{
            "id": 1,
            "domain_names": ["app.shearer.live"],
            "nice_name": "App Cert",
            "expires_on": expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }])

        await adapter._poll_certificates()

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.severity == Severity.ERROR
        assert event.payload["level"] == "critical"

    @pytest.mark.asyncio
    async def test_cert_ok_no_event(self) -> None:
        from datetime import UTC, datetime, timedelta

        adapter, queue = _make_adapter()
        expires = datetime.now(tz=UTC) + timedelta(days=90)

        adapter._client.get = AsyncMock(return_value=[{
            "id": 1,
            "domain_names": ["app.shearer.live"],
            "nice_name": "App Cert",
            "expires_on": expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }])

        await adapter._poll_certificates()
        queue.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_cert_renewed_event(self) -> None:
        from datetime import UTC, datetime, timedelta

        adapter, queue = _make_adapter()
        adapter._cert_alert_state[1] = "warning"
        expires = datetime.now(tz=UTC) + timedelta(days=90)

        adapter._client.get = AsyncMock(return_value=[{
            "id": 1,
            "domain_names": ["app.shearer.live"],
            "nice_name": "App Cert",
            "expires_on": expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }])

        await adapter._poll_certificates()

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "npm_certificate_renewed"
        assert event.severity == Severity.INFO

    @pytest.mark.asyncio
    async def test_cert_same_state_no_duplicate(self) -> None:
        from datetime import UTC, datetime, timedelta

        adapter, queue = _make_adapter()
        adapter._cert_alert_state[1] = "warning"
        expires = datetime.now(tz=UTC) + timedelta(days=10)

        adapter._client.get = AsyncMock(return_value=[{
            "id": 1,
            "domain_names": ["app.shearer.live"],
            "nice_name": "App Cert",
            "expires_on": expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }])

        await adapter._poll_certificates()
        queue.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_cert_warning_to_critical_transition(self) -> None:
        from datetime import UTC, datetime, timedelta

        adapter, queue = _make_adapter()
        adapter._cert_alert_state[1] = "warning"
        expires = datetime.now(tz=UTC) + timedelta(days=3)

        adapter._client.get = AsyncMock(return_value=[{
            "id": 1,
            "domain_names": ["app.shearer.live"],
            "nice_name": "App Cert",
            "expires_on": expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }])

        await adapter._poll_certificates()

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.severity == Severity.ERROR
        assert event.payload["level"] == "critical"

    @pytest.mark.asyncio
    async def test_cert_dedup_key(self) -> None:
        from datetime import UTC, datetime, timedelta

        adapter, queue = _make_adapter()
        expires = datetime.now(tz=UTC) + timedelta(days=5)

        adapter._client.get = AsyncMock(return_value=[{
            "id": 42,
            "domain_names": ["my.domain.com"],
            "nice_name": "My Cert",
            "expires_on": expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }])

        await adapter._poll_certificates()

        event = queue.put_nowait.call_args[0][0]
        assert event.metadata.dedup_key == "npm:cert:42:expiry"


# ---------------------------------------------------------------------------
# Dead host polling
# ---------------------------------------------------------------------------


class TestDeadHostPolling:
    @pytest.mark.asyncio
    async def test_new_dead_host_emits_event(self) -> None:
        adapter, queue = _make_adapter()

        adapter._client.get = AsyncMock(return_value=[{
            "id": 1,
            "domain_names": ["dead.shearer.live"],
        }])

        await adapter._poll_dead_hosts()

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "npm_dead_host_detected"
        assert event.severity == Severity.WARNING
        assert event.entity_id == "dead.shearer.live"
        assert event.payload["host_id"] == 1

    @pytest.mark.asyncio
    async def test_seen_dead_host_no_duplicate(self) -> None:
        adapter, queue = _make_adapter()
        adapter._seen_dead_hosts = {1}

        adapter._client.get = AsyncMock(return_value=[{
            "id": 1,
            "domain_names": ["dead.shearer.live"],
        }])

        await adapter._poll_dead_hosts()
        queue.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_cleared_dead_host_evicted(self) -> None:
        adapter, _queue = _make_adapter()
        adapter._seen_dead_hosts = {1, 2}

        adapter._client.get = AsyncMock(return_value=[{
            "id": 1,
            "domain_names": ["dead.shearer.live"],
        }])

        await adapter._poll_dead_hosts()
        assert adapter._seen_dead_hosts == {1}

    @pytest.mark.asyncio
    async def test_dead_host_dedup_key(self) -> None:
        adapter, queue = _make_adapter()

        adapter._client.get = AsyncMock(return_value=[{
            "id": 42,
            "domain_names": ["dead.example.com"],
        }])

        await adapter._poll_dead_hosts()

        event = queue.put_nowait.call_args[0][0]
        assert event.metadata.dedup_key == "npm:dead_host:42"

    @pytest.mark.asyncio
    async def test_empty_dead_hosts_no_event(self) -> None:
        adapter, queue = _make_adapter()

        adapter._client.get = AsyncMock(return_value=[])

        await adapter._poll_dead_hosts()
        queue.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_domain_names_uses_host_id(self) -> None:
        adapter, queue = _make_adapter()

        adapter._client.get = AsyncMock(return_value=[{
            "id": 99,
            "domain_names": [],
        }])

        await adapter._poll_dead_hosts()

        event = queue.put_nowait.call_args[0][0]
        assert event.entity_id == "99"


# ---------------------------------------------------------------------------
# Enqueue error handling
# ---------------------------------------------------------------------------


class TestEnqueueErrorHandling:
    def test_enqueue_failure_logged(self) -> None:
        adapter, queue = _make_adapter()
        queue.put_nowait.side_effect = RuntimeError("queue full")

        from datetime import UTC, datetime

        from oasisagent.models import Event, EventMetadata

        event = Event(
            source="npm",
            system="npm",
            event_type="test",
            entity_id="test",
            severity=Severity.INFO,
            timestamp=datetime.now(tz=UTC),
            payload={},
            metadata=EventMetadata(dedup_key="test"),
        )

        adapter._enqueue(event)  # should not raise


# ---------------------------------------------------------------------------
# Known fixes YAML
# ---------------------------------------------------------------------------


class TestKnownFixes:
    def test_known_fixes_file_exists(self) -> None:
        from pathlib import Path

        import yaml

        fixes_path = (
            Path(__file__).parent.parent.parent / "known_fixes" / "npm.yaml"
        )
        assert fixes_path.exists()

        with fixes_path.open() as f:
            data = yaml.safe_load(f)

        assert "fixes" in data
        fixes = data["fixes"]
        assert len(fixes) >= 3

        for fix in fixes:
            assert "id" in fix
            assert "match" in fix
            assert fix["match"]["system"] == "npm"
            assert "diagnosis" in fix
            assert "action" in fix
            assert "risk_tier" in fix

    def test_known_fix_ids_unique(self) -> None:
        from pathlib import Path

        import yaml

        fixes_path = (
            Path(__file__).parent.parent.parent / "known_fixes" / "npm.yaml"
        )
        with fixes_path.open() as f:
            data = yaml.safe_load(f)

        ids = [fix["id"] for fix in data["fixes"]]
        assert len(ids) == len(set(ids))

    def test_known_fix_event_types_match_adapter(self) -> None:
        from pathlib import Path

        import yaml

        fixes_path = (
            Path(__file__).parent.parent.parent / "known_fixes" / "npm.yaml"
        )
        with fixes_path.open() as f:
            data = yaml.safe_load(f)

        expected_types = {
            "npm_proxy_host_error",
            "npm_certificate_expiring",
            "npm_dead_host_detected",
        }

        fix_types = {fix["match"]["event_type"] for fix in data["fixes"]}
        assert fix_types == expected_types


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_npm_connector_registered(self) -> None:
        from oasisagent.db.registry import CONNECTOR_TYPES

        assert "npm" in CONNECTOR_TYPES
        meta = CONNECTOR_TYPES["npm"]
        assert meta.model is NpmAdapterConfig
        assert "password" in meta.secret_fields
