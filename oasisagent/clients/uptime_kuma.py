"""Uptime Kuma HTTP client for Prometheus metrics endpoint.

Fetches monitor data from Uptime Kuma's ``/metrics`` endpoint using
HTTP Basic auth (empty username, API key as password). This is the
API Key auth model available in Uptime Kuma v1.21+; earlier versions
used session cookies which this client does not support.

The client parses Prometheus text exposition format, targeting only the
four known metric names:
- ``monitor_status`` (1=up, 0=down)
- ``monitor_response_time`` (milliseconds)
- ``monitor_cert_days_remaining``
- ``monitor_cert_is_valid`` (1=valid, 0=invalid)

Unrecognized lines (``# HELP``, ``# TYPE``, other metrics) are skipped.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass

import aiohttp

logger = logging.getLogger(__name__)

# Matches Prometheus metric lines like:
#   monitor_status{monitor_name="Google",monitor_type="http",monitor_url="https://google.com"} 1
_METRIC_RE = re.compile(
    r'^(?P<metric>\w+)\{(?P<labels>[^}]*)\}\s+(?P<value>\S+)$'
)

# Labels we extract from Prometheus metric lines
_LABEL_RE = re.compile(r'(\w+)="([^"]*)"')

# The four metrics we care about
_KNOWN_METRICS = frozenset({
    "monitor_status",
    "monitor_response_time",
    "monitor_cert_days_remaining",
    "monitor_cert_is_valid",
})


@dataclass
class MonitorMetrics:
    """Parsed metrics for a single Uptime Kuma monitor."""

    name: str
    monitor_type: str
    url: str
    hostname: str
    port: str
    status: int  # 1=up, 0=down
    response_time_ms: float | None = None
    cert_days_remaining: int | None = None
    cert_is_valid: bool | None = None


class UptimeKumaClient:
    """HTTP client for Uptime Kuma's Prometheus metrics endpoint.

    Args:
        url: Base URL of the Uptime Kuma instance (e.g., http://uptime-kuma:3001).
        api_key: API key for authentication (used as Basic auth password).
        timeout: Request timeout in seconds.
    """

    def __init__(self, url: str, api_key: str, timeout: int = 10) -> None:
        self._base_url = url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout
        self._session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        """Create the HTTP session."""
        auth = aiohttp.BasicAuth(login="", password=self._api_key)
        timeout = aiohttp.ClientTimeout(total=self._timeout)
        self._session = aiohttp.ClientSession(auth=auth, timeout=timeout)

    async def close(self) -> None:
        """Close the HTTP session."""
        if self._session:
            await self._session.close()
            self._session = None

    async def fetch_metrics(self) -> list[MonitorMetrics]:
        """Fetch and parse monitor metrics from /metrics endpoint.

        Returns:
            List of MonitorMetrics, one per monitor found in the response.

        Raises:
            aiohttp.ClientError: On connection or HTTP errors.
            RuntimeError: If the client session is not started.
        """
        if self._session is None:
            msg = "Client not started — call start() first"
            raise RuntimeError(msg)

        async with self._session.get(f"{self._base_url}/metrics") as resp:
            resp.raise_for_status()
            body = await resp.text()

        return self._parse_metrics(body)

    def _parse_metrics(self, body: str) -> list[MonitorMetrics]:
        """Parse Prometheus text exposition format into MonitorMetrics.

        Only processes the four known metric names. Builds a monitor dict
        keyed by monitor_name, then converts to MonitorMetrics objects.
        """
        # Intermediate: monitor_name -> {metric_name: value, labels...}
        monitors: dict[str, dict[str, str | float]] = {}

        for line in body.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            match = _METRIC_RE.match(line)
            if not match:
                continue

            metric_name = match.group("metric")
            if metric_name not in _KNOWN_METRICS:
                continue

            labels = dict(_LABEL_RE.findall(match.group("labels")))
            value = match.group("value")
            monitor_name = labels.get("monitor_name", "")
            if not monitor_name:
                continue

            if monitor_name not in monitors:
                monitors[monitor_name] = {
                    "monitor_type": labels.get("monitor_type", ""),
                    "monitor_url": labels.get("monitor_url", ""),
                    "monitor_hostname": labels.get("monitor_hostname", ""),
                    "monitor_port": labels.get("monitor_port", ""),
                }

            monitors[monitor_name][metric_name] = value

        result: list[MonitorMetrics] = []
        for name, data in monitors.items():
            status_raw = data.get("monitor_status")
            if status_raw is None:
                continue  # Must have status to be useful

            response_time = data.get("monitor_response_time")
            cert_days = data.get("monitor_cert_days_remaining")
            cert_valid = data.get("monitor_cert_is_valid")

            # Prometheus gauge values are always text. int(float(str(v)))
            # handles both integer ("1") and float ("1.0") representations.
            # Guard against NaN/Inf which Prometheus can emit for no-data.
            status_f = float(str(status_raw))
            if math.isnan(status_f) or math.isinf(status_f):
                continue  # No usable status — skip monitor

            resp_f = (
                float(str(response_time)) if response_time is not None else None
            )
            if resp_f is not None and (math.isnan(resp_f) or math.isinf(resp_f)):
                resp_f = None

            cert_d: int | None = None
            if cert_days is not None:
                cert_f = float(str(cert_days))
                if not (math.isnan(cert_f) or math.isinf(cert_f)):
                    cert_d = int(cert_f)

            cert_v: bool | None = None
            if cert_valid is not None:
                cv_f = float(str(cert_valid))
                if not (math.isnan(cv_f) or math.isinf(cv_f)):
                    cert_v = int(cv_f) == 1

            result.append(MonitorMetrics(
                name=name,
                monitor_type=str(data.get("monitor_type", "")),
                url=str(data.get("monitor_url", "")),
                hostname=str(data.get("monitor_hostname", "")),
                port=str(data.get("monitor_port", "")),
                status=int(status_f),
                response_time_ms=resp_f,
                cert_days_remaining=cert_d,
                cert_is_valid=cert_v,
            ))

        return result
