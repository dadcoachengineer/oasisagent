"""Tests for audit writer retry logic (#146)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from influxdb_client import Point
from influxdb_client.rest import ApiException

from oasisagent.audit.influxdb import AuditWriter
from oasisagent.config import AuditConfig, InfluxDbConfig


def _make_config() -> AuditConfig:
    return AuditConfig(
        influxdb=InfluxDbConfig(
            enabled=True,
            url="http://localhost:8086",
            token="test-token",
            org="testorg",
            bucket="testbucket",
        ),
    )


def _make_writer() -> tuple[AuditWriter, AsyncMock]:
    """Create an AuditWriter with a mocked write API."""
    writer = AuditWriter(_make_config())
    mock_write_api = AsyncMock()
    writer._write_api = mock_write_api
    return writer, mock_write_api


class TestAuditRetry:
    @pytest.mark.asyncio
    @patch("oasisagent.audit.influxdb.asyncio.sleep", new_callable=AsyncMock)
    async def test_retry_on_transient_failure(self, mock_sleep: AsyncMock) -> None:
        """Write fails once then succeeds → record written on retry."""
        writer, mock_api = _make_writer()
        mock_api.write = AsyncMock(
            side_effect=[ConnectionError("refused"), None]
        )

        point = Point("test")
        await writer._write(point, measurement="test_m")

        assert mock_api.write.call_count == 2
        mock_sleep.assert_called_once_with(0.5)

    @pytest.mark.asyncio
    @patch("oasisagent.audit.influxdb.asyncio.sleep", new_callable=AsyncMock)
    async def test_drop_after_max_retries(self, mock_sleep: AsyncMock) -> None:
        """Write fails 3 times → dropped after 3 attempts, no exception."""
        writer, mock_api = _make_writer()
        mock_api.write = AsyncMock(
            side_effect=ConnectionError("refused")
        )

        point = Point("test")
        await writer._write(point, measurement="test_m")

        assert mock_api.write.call_count == 3
        assert mock_sleep.call_count == 2

    @pytest.mark.asyncio
    @patch("oasisagent.audit.influxdb.asyncio.sleep", new_callable=AsyncMock)
    async def test_no_retry_on_401(self, mock_sleep: AsyncMock) -> None:
        """Write fails with 401 ApiException → no retry, dropped."""
        writer, mock_api = _make_writer()
        exc = ApiException(status=401, reason="Unauthorized")
        mock_api.write = AsyncMock(side_effect=exc)

        point = Point("test")
        await writer._write(point, measurement="test_m")

        assert mock_api.write.call_count == 1
        mock_sleep.assert_not_called()

    @pytest.mark.asyncio
    @patch("oasisagent.audit.influxdb.asyncio.sleep", new_callable=AsyncMock)
    async def test_success_first_try(self, mock_sleep: AsyncMock) -> None:
        """Write succeeds first try → no retry invoked."""
        writer, mock_api = _make_writer()
        mock_api.write = AsyncMock(return_value=None)

        point = Point("test")
        await writer._write(point, measurement="test_m")

        assert mock_api.write.call_count == 1
        mock_sleep.assert_not_called()

    @pytest.mark.asyncio
    @patch("oasisagent.audit.influxdb.asyncio.sleep", new_callable=AsyncMock)
    async def test_retry_on_5xx(self, mock_sleep: AsyncMock) -> None:
        """Write fails with 503 then succeeds → retried."""
        writer, mock_api = _make_writer()
        exc = ApiException(status=503, reason="Service Unavailable")
        mock_api.write = AsyncMock(side_effect=[exc, None])

        point = Point("test")
        await writer._write(point, measurement="test_m")

        assert mock_api.write.call_count == 2

    @pytest.mark.asyncio
    @patch("oasisagent.audit.influxdb.asyncio.sleep", new_callable=AsyncMock)
    async def test_retry_on_429(self, mock_sleep: AsyncMock) -> None:
        """Write fails with 429 then succeeds → retried."""
        writer, mock_api = _make_writer()
        exc = ApiException(status=429, reason="Too Many Requests")
        mock_api.write = AsyncMock(side_effect=[exc, None])

        point = Point("test")
        await writer._write(point, measurement="test_m")

        assert mock_api.write.call_count == 2


class TestIsRetryable:
    def test_connection_error_is_retryable(self) -> None:
        assert AuditWriter._is_retryable(ConnectionError()) is True

    def test_timeout_error_is_retryable(self) -> None:
        assert AuditWriter._is_retryable(TimeoutError()) is True

    def test_api_5xx_is_retryable(self) -> None:
        exc = ApiException(status=500, reason="Internal Server Error")
        assert AuditWriter._is_retryable(exc) is True

    def test_api_429_is_retryable(self) -> None:
        exc = ApiException(status=429, reason="Too Many Requests")
        assert AuditWriter._is_retryable(exc) is True

    def test_api_401_not_retryable(self) -> None:
        exc = ApiException(status=401, reason="Unauthorized")
        assert AuditWriter._is_retryable(exc) is False

    def test_api_400_not_retryable(self) -> None:
        exc = ApiException(status=400, reason="Bad Request")
        assert AuditWriter._is_retryable(exc) is False

    def test_generic_exception_not_retryable(self) -> None:
        assert AuditWriter._is_retryable(ValueError("oops")) is False


class TestNotificationArchive:
    @pytest.mark.asyncio
    async def test_write_notification_archive(self) -> None:
        """Archive a notification row to InfluxDB."""
        writer, mock_api = _make_writer()
        mock_api.write = AsyncMock(return_value=None)

        row = {
            "id": "notif-123",
            "event_id": "evt-456",
            "severity": "warning",
            "title": "Test alert",
            "message": "Something happened",
            "metadata": {"entity_id": "sensor.test"},
        }
        await writer.write_notification_archive(row)

        assert mock_api.write.call_count == 1

    @pytest.mark.asyncio
    async def test_write_notification_archive_disabled(self) -> None:
        """No-op when InfluxDB is disabled."""
        config = AuditConfig(
            influxdb=InfluxDbConfig(enabled=False),
        )
        writer = AuditWriter(config)
        # Should not raise
        await writer.write_notification_archive({"id": "x", "severity": "info"})
