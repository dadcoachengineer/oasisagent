"""Tests for the Cloudflare polling ingestion adapter."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from oasisagent.config import CloudflareAdapterConfig
from oasisagent.ingestion.cloudflare import CloudflareAdapter
from oasisagent.models import Severity


def _make_config(**overrides: object) -> CloudflareAdapterConfig:
    defaults: dict[str, object] = {
        "enabled": True,
        "api_token": "test-token",
        "account_id": "acc-123",
        "zone_id": "zone-456",
        "poll_interval": 300,
        "poll_tunnels": True,
        "poll_waf": True,
        "poll_ssl": True,
        "waf_lookback_minutes": 10,
        "waf_spike_threshold": 50,
        "timeout": 10,
    }
    defaults.update(overrides)
    return CloudflareAdapterConfig(**defaults)  # type: ignore[arg-type]


def _mock_queue() -> MagicMock:
    return MagicMock()


def _make_adapter(
    **overrides: object,
) -> tuple[CloudflareAdapter, MagicMock]:
    config = _make_config(**overrides)
    queue = _mock_queue()
    with patch("oasisagent.ingestion.cloudflare.CloudflareClient"):
        adapter = CloudflareAdapter(config, queue)
    return adapter, queue


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


class TestConfigValidation:
    def test_defaults(self) -> None:
        config = CloudflareAdapterConfig()
        assert config.enabled is False
        assert config.poll_interval == 300
        assert config.waf_lookback_minutes == 10
        assert config.waf_spike_threshold == 50

    def test_extra_fields_forbidden(self) -> None:
        with pytest.raises(ValidationError, match="Extra inputs"):
            _make_config(bogus="nope")


# ---------------------------------------------------------------------------
# Adapter identity & lifecycle
# ---------------------------------------------------------------------------


class TestAdapterLifecycle:
    def test_name(self) -> None:
        adapter, _ = _make_adapter()
        assert adapter.name == "cloudflare"

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
        assert adapter._connected is False

    @pytest.mark.asyncio
    async def test_start_connection_failure(self) -> None:
        adapter, _ = _make_adapter()
        adapter._client.start = AsyncMock(
            side_effect=ConnectionError("refused"),
        )
        await adapter.start()
        assert adapter._connected is False


# ---------------------------------------------------------------------------
# Tunnel polling
# ---------------------------------------------------------------------------


class TestTunnelPolling:
    @pytest.mark.asyncio
    async def test_tunnel_disconnect_event(self) -> None:
        adapter, queue = _make_adapter()
        adapter._tunnel_states["t1"] = "active"

        adapter._client.get = AsyncMock(return_value={
            "result": [{
                "id": "t1",
                "name": "my-tunnel",
                "status": "inactive",
            }],
        })

        await adapter._poll_tunnels()

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "tunnel_disconnected"
        assert event.severity == Severity.ERROR
        assert event.entity_id == "my-tunnel"
        assert event.payload["status"] == "inactive"
        assert event.payload["previous_status"] == "active"

    @pytest.mark.asyncio
    async def test_tunnel_recovery_event(self) -> None:
        adapter, queue = _make_adapter()
        adapter._tunnel_states["t1"] = "inactive"

        adapter._client.get = AsyncMock(return_value={
            "result": [{
                "id": "t1",
                "name": "my-tunnel",
                "status": "active",
            }],
        })

        await adapter._poll_tunnels()

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "tunnel_recovered"
        assert event.severity == Severity.INFO

    @pytest.mark.asyncio
    async def test_same_status_no_event(self) -> None:
        adapter, queue = _make_adapter()
        adapter._tunnel_states["t1"] = "active"

        adapter._client.get = AsyncMock(return_value={
            "result": [{"id": "t1", "name": "my-tunnel", "status": "active"}],
        })

        await adapter._poll_tunnels()
        queue.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_first_poll_active_no_event(self) -> None:
        adapter, queue = _make_adapter()

        adapter._client.get = AsyncMock(return_value={
            "result": [{"id": "t1", "name": "my-tunnel", "status": "active"}],
        })

        await adapter._poll_tunnels()
        queue.put_nowait.assert_not_called()
        assert adapter._tunnel_states["t1"] == "active"

    @pytest.mark.asyncio
    async def test_first_poll_inactive_emits(self) -> None:
        adapter, queue = _make_adapter()

        adapter._client.get = AsyncMock(return_value={
            "result": [{
                "id": "t1",
                "name": "my-tunnel",
                "status": "down",
            }],
        })

        await adapter._poll_tunnels()

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "tunnel_disconnected"

    @pytest.mark.asyncio
    async def test_empty_tunnel_id_skipped(self) -> None:
        adapter, queue = _make_adapter()

        adapter._client.get = AsyncMock(return_value={
            "result": [{"id": "", "name": "bad", "status": "active"}],
        })

        await adapter._poll_tunnels()
        queue.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_tunnel_dedup_key(self) -> None:
        adapter, queue = _make_adapter()
        adapter._tunnel_states["t1"] = "active"

        adapter._client.get = AsyncMock(return_value={
            "result": [{"id": "t1", "name": "tun", "status": "inactive"}],
        })

        await adapter._poll_tunnels()

        event = queue.put_nowait.call_args[0][0]
        assert event.metadata.dedup_key == "cloudflare:tunnel:t1:status"

    @pytest.mark.asyncio
    async def test_deleted_tunnel_evicted_from_tracker(self) -> None:
        """Tunnels no longer in the response are evicted from state tracker."""
        adapter, _queue = _make_adapter()
        adapter._tunnel_states = {"t1": "active", "t2": "active"}

        adapter._client.get = AsyncMock(return_value={
            "result": [{"id": "t1", "name": "tun-1", "status": "active"}],
        })

        await adapter._poll_tunnels()

        assert "t1" in adapter._tunnel_states
        assert "t2" not in adapter._tunnel_states


# ---------------------------------------------------------------------------
# WAF polling
# ---------------------------------------------------------------------------


class TestWafPolling:
    @pytest.mark.asyncio
    async def test_waf_spike_event(self) -> None:
        adapter, queue = _make_adapter(waf_spike_threshold=3)

        # 5 blocked events — above threshold of 3
        waf_events = [
            {"action": "block", "ruleId": "r1", "clientIP": "1.2.3.4"},
            {"action": "block", "ruleId": "r1", "clientIP": "1.2.3.4"},
            {"action": "block", "ruleId": "r2", "clientIP": "5.6.7.8"},
            {"action": "challenge", "ruleId": "r2", "clientIP": "5.6.7.8"},
            {"action": "managed_challenge", "ruleId": "r3", "clientIP": "9.0.1.2"},
        ]

        adapter._client.get = AsyncMock(return_value={
            "result": waf_events,
        })

        await adapter._poll_waf()

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "waf_spike"
        assert event.severity == Severity.WARNING
        assert event.payload["blocked_count"] == 5
        assert event.payload["unique_source_ips"] == 3
        assert "r1" in event.payload["top_rules"]

    @pytest.mark.asyncio
    async def test_waf_below_threshold_no_event(self) -> None:
        adapter, queue = _make_adapter(waf_spike_threshold=10)

        adapter._client.get = AsyncMock(return_value={
            "result": [
                {"action": "block", "ruleId": "r1", "clientIP": "1.2.3.4"},
            ],
        })

        await adapter._poll_waf()
        queue.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_waf_allow_actions_not_counted(self) -> None:
        adapter, queue = _make_adapter(waf_spike_threshold=3)

        adapter._client.get = AsyncMock(return_value={
            "result": [
                {"action": "allow", "ruleId": "r1", "clientIP": "1.2.3.4"},
                {"action": "log", "ruleId": "r2", "clientIP": "5.6.7.8"},
                {"action": "block", "ruleId": "r3", "clientIP": "9.0.1.2"},
            ],
        })

        await adapter._poll_waf()
        queue.put_nowait.assert_not_called()  # only 1 blocked

    @pytest.mark.asyncio
    async def test_waf_empty_response_no_event(self) -> None:
        adapter, queue = _make_adapter()

        adapter._client.get = AsyncMock(return_value={"result": []})

        await adapter._poll_waf()
        queue.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_waf_lookback_uses_last_poll_time(self) -> None:
        """Second poll should use last poll time, not lookback window."""
        from datetime import UTC, datetime

        adapter, _queue = _make_adapter(waf_spike_threshold=100)
        first_time = datetime(2026, 3, 12, 10, 0, 0, tzinfo=UTC)
        adapter._last_waf_poll = first_time

        adapter._client.get = AsyncMock(return_value={"result": []})

        await adapter._poll_waf()

        call_kwargs = adapter._client.get.call_args[1]
        assert call_kwargs["since"] == "2026-03-12T10:00:00Z"

    @pytest.mark.asyncio
    async def test_waf_dedup_key(self) -> None:
        adapter, queue = _make_adapter(waf_spike_threshold=1)

        adapter._client.get = AsyncMock(return_value={
            "result": [
                {"action": "block", "ruleId": "r1", "clientIP": "1.2.3.4"},
            ],
        })

        await adapter._poll_waf()

        event = queue.put_nowait.call_args[0][0]
        assert event.metadata.dedup_key == "cloudflare:waf:zone-456:spike"


# ---------------------------------------------------------------------------
# SSL certificate polling
# ---------------------------------------------------------------------------


class TestSslPolling:
    @pytest.mark.asyncio
    async def test_ssl_warning_event(self) -> None:
        from datetime import UTC, datetime, timedelta

        adapter, queue = _make_adapter()
        expires = datetime.now(tz=UTC) + timedelta(days=20)

        adapter._client.get = AsyncMock(return_value={
            "result": [{
                "hosts": ["example.com"],
                "certificates": [{
                    "expires_on": expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
                }],
                "type": "universal",
                "status": "active",
            }],
        })

        await adapter._poll_ssl()

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "certificate_expiry"
        assert event.severity == Severity.WARNING
        assert event.entity_id == "example.com"
        assert event.payload["level"] == "warning"

    @pytest.mark.asyncio
    async def test_ssl_critical_event(self) -> None:
        from datetime import UTC, datetime, timedelta

        adapter, queue = _make_adapter()
        expires = datetime.now(tz=UTC) + timedelta(days=3)

        adapter._client.get = AsyncMock(return_value={
            "result": [{
                "hosts": ["example.com"],
                "certificates": [{
                    "expires_on": expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
                }],
                "type": "universal",
                "status": "active",
            }],
        })

        await adapter._poll_ssl()

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.severity == Severity.ERROR
        assert event.payload["level"] == "critical"

    @pytest.mark.asyncio
    async def test_ssl_ok_no_event(self) -> None:
        from datetime import UTC, datetime, timedelta

        adapter, queue = _make_adapter()
        expires = datetime.now(tz=UTC) + timedelta(days=90)

        adapter._client.get = AsyncMock(return_value={
            "result": [{
                "hosts": ["example.com"],
                "certificates": [{
                    "expires_on": expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
                }],
                "type": "universal",
                "status": "active",
            }],
        })

        await adapter._poll_ssl()
        queue.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_ssl_renewal_event(self) -> None:
        """Certificate renewed after being in warning state."""
        from datetime import UTC, datetime, timedelta

        adapter, queue = _make_adapter()
        adapter._ssl_alert_state["example.com"] = "warning"
        expires = datetime.now(tz=UTC) + timedelta(days=90)

        adapter._client.get = AsyncMock(return_value={
            "result": [{
                "hosts": ["example.com"],
                "certificates": [{
                    "expires_on": expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
                }],
                "type": "universal",
                "status": "active",
            }],
        })

        await adapter._poll_ssl()

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "certificate_renewed"
        assert event.severity == Severity.INFO

    @pytest.mark.asyncio
    async def test_ssl_same_state_no_duplicate(self) -> None:
        from datetime import UTC, datetime, timedelta

        adapter, queue = _make_adapter()
        adapter._ssl_alert_state["example.com"] = "warning"
        expires = datetime.now(tz=UTC) + timedelta(days=20)

        adapter._client.get = AsyncMock(return_value={
            "result": [{
                "hosts": ["example.com"],
                "certificates": [{
                    "expires_on": expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
                }],
                "type": "universal",
                "status": "active",
            }],
        })

        await adapter._poll_ssl()
        queue.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_ssl_warning_to_critical_transition(self) -> None:
        from datetime import UTC, datetime, timedelta

        adapter, queue = _make_adapter()
        adapter._ssl_alert_state["example.com"] = "warning"
        expires = datetime.now(tz=UTC) + timedelta(days=3)

        adapter._client.get = AsyncMock(return_value={
            "result": [{
                "hosts": ["example.com"],
                "certificates": [{
                    "expires_on": expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
                }],
                "type": "universal",
                "status": "active",
            }],
        })

        await adapter._poll_ssl()

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.severity == Severity.ERROR
        assert event.payload["level"] == "critical"

    @pytest.mark.asyncio
    async def test_ssl_dedup_key(self) -> None:
        from datetime import UTC, datetime, timedelta

        adapter, queue = _make_adapter()
        expires = datetime.now(tz=UTC) + timedelta(days=5)

        adapter._client.get = AsyncMock(return_value={
            "result": [{
                "hosts": ["my.domain.com"],
                "certificates": [{
                    "expires_on": expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
                }],
                "type": "universal",
                "status": "active",
            }],
        })

        await adapter._poll_ssl()

        event = queue.put_nowait.call_args[0][0]
        assert event.metadata.dedup_key == "cloudflare:ssl:my.domain.com"


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
            source="cloudflare",
            system="cloudflare",
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
            Path(__file__).parent.parent.parent / "known_fixes" / "cloudflare.yaml"
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
            assert fix["match"]["system"] == "cloudflare"
            assert "diagnosis" in fix
            assert "action" in fix
            assert "risk_tier" in fix

    def test_known_fix_ids_unique(self) -> None:
        from pathlib import Path

        import yaml

        fixes_path = (
            Path(__file__).parent.parent.parent / "known_fixes" / "cloudflare.yaml"
        )
        with fixes_path.open() as f:
            data = yaml.safe_load(f)

        ids = [fix["id"] for fix in data["fixes"]]
        assert len(ids) == len(set(ids))

    def test_known_fix_event_types_match_adapter(self) -> None:
        from pathlib import Path

        import yaml

        fixes_path = (
            Path(__file__).parent.parent.parent / "known_fixes" / "cloudflare.yaml"
        )
        with fixes_path.open() as f:
            data = yaml.safe_load(f)

        expected_types = {
            "tunnel_disconnected",
            "waf_spike",
            "certificate_expiry",
        }

        fix_types = {fix["match"]["event_type"] for fix in data["fixes"]}
        assert fix_types == expected_types


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_cloudflare_connector_registered(self) -> None:
        from oasisagent.db.registry import CONNECTOR_TYPES

        assert "cloudflare" in CONNECTOR_TYPES
        meta = CONNECTOR_TYPES["cloudflare"]
        assert meta.model is CloudflareAdapterConfig
        assert "api_token" in meta.secret_fields
