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
        assert all(v is False for v in adapter._endpoint_health.values())

    @pytest.mark.asyncio
    async def test_start_connection_failure(self) -> None:
        adapter, _ = _make_adapter()
        adapter._client.start = AsyncMock(
            side_effect=ConnectionError("refused"),
        )
        await adapter.start()
        assert not await adapter.healthy()


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


def _graphql_waf_response(
    events: list[dict[str, object]],
) -> dict[str, object]:
    """Wrap WAF events in the Cloudflare GraphQL response envelope."""
    return {
        "data": {
            "viewer": {
                "zones": [{"firewallEventsAdaptive": events}],
            },
        },
        "errors": None,
    }


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

        adapter._client.graphql = AsyncMock(
            return_value=_graphql_waf_response(waf_events),
        )

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

        adapter._client.graphql = AsyncMock(
            return_value=_graphql_waf_response([
                {"action": "block", "ruleId": "r1", "clientIP": "1.2.3.4"},
            ]),
        )

        await adapter._poll_waf()
        queue.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_waf_allow_actions_not_counted(self) -> None:
        adapter, queue = _make_adapter(waf_spike_threshold=3)

        adapter._client.graphql = AsyncMock(
            return_value=_graphql_waf_response([
                {"action": "allow", "ruleId": "r1", "clientIP": "1.2.3.4"},
                {"action": "log", "ruleId": "r2", "clientIP": "5.6.7.8"},
                {"action": "block", "ruleId": "r3", "clientIP": "9.0.1.2"},
            ]),
        )

        await adapter._poll_waf()
        queue.put_nowait.assert_not_called()  # only 1 blocked

    @pytest.mark.asyncio
    async def test_waf_empty_response_no_event(self) -> None:
        adapter, queue = _make_adapter()

        adapter._client.graphql = AsyncMock(
            return_value=_graphql_waf_response([]),
        )

        await adapter._poll_waf()
        queue.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_waf_lookback_uses_last_poll_time(self) -> None:
        """Second poll should use datetime_gt in query string."""
        from datetime import UTC, datetime

        adapter, _queue = _make_adapter(waf_spike_threshold=100)
        first_time = datetime(2026, 3, 12, 10, 0, 0, tzinfo=UTC)
        adapter._last_waf_poll = first_time

        adapter._client.graphql = AsyncMock(
            return_value=_graphql_waf_response([]),
        )

        await adapter._poll_waf()

        query_arg = adapter._client.graphql.call_args[0][0]
        assert "2026-03-12T10:00:00Z" in query_arg

    @pytest.mark.asyncio
    async def test_waf_dedup_key(self) -> None:
        adapter, queue = _make_adapter(waf_spike_threshold=1)

        adapter._client.graphql = AsyncMock(
            return_value=_graphql_waf_response([
                {"action": "block", "ruleId": "r1", "clientIP": "1.2.3.4"},
            ]),
        )

        await adapter._poll_waf()

        event = queue.put_nowait.call_args[0][0]
        assert event.metadata.dedup_key == "cloudflare:waf:zone-456:spike"

    @pytest.mark.asyncio
    async def test_waf_empty_zones_no_event(self) -> None:
        """Invalid zone tag returns empty zones list — should not crash."""
        adapter, queue = _make_adapter()

        adapter._client.graphql = AsyncMock(return_value={
            "data": {"viewer": {"zones": []}},
            "errors": None,
        })

        await adapter._poll_waf()
        queue.put_nowait.assert_not_called()


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
# Per-endpoint health tracking
# ---------------------------------------------------------------------------


class TestEndpointHealth:
    def test_endpoint_health_initial_state(self) -> None:
        """All endpoints start False before first poll."""
        adapter, _ = _make_adapter()
        assert adapter._endpoint_health == {
            "tunnels": False, "waf": False, "ssl": False,
        }

    @pytest.mark.asyncio
    async def test_healthy_any_endpoint_up(self) -> None:
        adapter, _ = _make_adapter()
        adapter._endpoint_health["tunnels"] = True
        adapter._endpoint_health["waf"] = False
        assert await adapter.healthy() is True

    @pytest.mark.asyncio
    async def test_healthy_all_endpoints_down(self) -> None:
        adapter, _ = _make_adapter()
        # All start False
        assert await adapter.healthy() is False

    @pytest.mark.asyncio
    async def test_healthy_no_endpoints_enabled(self) -> None:
        adapter, _ = _make_adapter(
            poll_tunnels=False, poll_waf=False, poll_ssl=False,
        )
        assert adapter._endpoint_health == {}
        assert await adapter.healthy() is False

    @pytest.mark.asyncio
    async def test_poll_waf_failure_isolates_tunnels_ssl(self) -> None:
        """WAF failure does not affect tunnels or SSL health."""
        adapter, _ = _make_adapter()

        async def _fail_waf() -> None:
            msg = "waf 400"
            raise RuntimeError(msg)

        adapter._poll_tunnels = AsyncMock()  # type: ignore[method-assign]
        adapter._poll_waf = _fail_waf  # type: ignore[method-assign]
        adapter._poll_ssl = AsyncMock()  # type: ignore[method-assign]

        # Run one iteration then stop
        adapter._stopping = False

        async def _stop_after_one(*_args: object, **_kwargs: object) -> None:
            adapter._stopping = True

        with patch("asyncio.sleep", new=_stop_after_one):
            await adapter._poll_loop()

        assert adapter._endpoint_health["tunnels"] is True
        assert adapter._endpoint_health["waf"] is False
        assert adapter._endpoint_health["ssl"] is True

    @pytest.mark.asyncio
    async def test_poll_tunnel_failure_isolates_waf_ssl(self) -> None:
        """Tunnel failure does not affect WAF or SSL health."""
        adapter, _ = _make_adapter()

        async def _fail_tunnels() -> None:
            msg = "tunnel timeout"
            raise RuntimeError(msg)

        adapter._poll_tunnels = _fail_tunnels  # type: ignore[method-assign]
        adapter._poll_waf = AsyncMock()  # type: ignore[method-assign]
        adapter._poll_ssl = AsyncMock()  # type: ignore[method-assign]

        adapter._stopping = False

        async def _stop_after_one(*_args: object, **_kwargs: object) -> None:
            adapter._stopping = True

        with patch("asyncio.sleep", new=_stop_after_one):
            await adapter._poll_loop()

        assert adapter._endpoint_health["tunnels"] is False
        assert adapter._endpoint_health["waf"] is True
        assert adapter._endpoint_health["ssl"] is True

    @pytest.mark.asyncio
    async def test_all_polls_fail_healthy_false(self) -> None:
        """All endpoints fail → healthy() returns False."""
        adapter, _ = _make_adapter()

        async def _fail() -> None:
            msg = "boom"
            raise RuntimeError(msg)

        adapter._poll_tunnels = _fail  # type: ignore[method-assign]
        adapter._poll_waf = _fail  # type: ignore[method-assign]
        adapter._poll_ssl = _fail  # type: ignore[method-assign]

        adapter._stopping = False

        async def _stop_after_one(*_args: object, **_kwargs: object) -> None:
            adapter._stopping = True

        with patch("asyncio.sleep", new=_stop_after_one):
            await adapter._poll_loop()

        assert await adapter.healthy() is False

    def test_health_detail_returns_per_endpoint_status(self) -> None:
        """Mixed health returns correct connected/disconnected dict."""
        adapter, _ = _make_adapter()
        adapter._endpoint_health["tunnels"] = True
        adapter._endpoint_health["waf"] = False
        adapter._endpoint_health["ssl"] = True

        detail = adapter.health_detail()
        assert detail == {
            "tunnels": "connected",
            "waf": "disconnected",
            "ssl": "connected",
        }

    @pytest.mark.asyncio
    async def test_endpoint_health_recovery(self) -> None:
        """Endpoint fails one cycle, succeeds next → flips to True."""
        adapter, _ = _make_adapter(
            poll_tunnels=True, poll_waf=False, poll_ssl=False,
            account_id="acc-123", zone_id="",
            poll_interval=1,
        )

        call_count = 0

        async def _fail_then_succeed() -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                msg = "first fail"
                raise RuntimeError(msg)

        adapter._poll_tunnels = _fail_then_succeed  # type: ignore[method-assign]

        sleep_calls = 0

        async def _stop_after_two_iterations(
            *_args: object, **_kwargs: object,
        ) -> None:
            nonlocal sleep_calls
            sleep_calls += 1
            if sleep_calls >= 2:
                adapter._stopping = True

        with patch("asyncio.sleep", new=_stop_after_two_iterations):
            await adapter._poll_loop()

        assert adapter._endpoint_health["tunnels"] is True

    @pytest.mark.asyncio
    async def test_stop_clears_endpoint_health(self) -> None:
        """After stop(), all endpoints are False."""
        adapter, _ = _make_adapter()
        adapter._endpoint_health["tunnels"] = True
        adapter._endpoint_health["waf"] = True
        adapter._endpoint_health["ssl"] = True

        adapter._client = AsyncMock()
        await adapter.stop()

        assert all(v is False for v in adapter._endpoint_health.values())


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
