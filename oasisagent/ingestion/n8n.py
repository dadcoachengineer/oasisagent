"""N8N workflow execution health monitoring adapter.

Polls the N8N REST API for service health and failed workflow executions.

Events emitted:
- ``n8n_unreachable`` (ERROR) when N8N API is not reachable
- ``n8n_recovered`` (INFO) when N8N API connectivity is restored
- ``n8n_execution_failed`` (WARNING) when a workflow execution has failed
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import aiohttp

from oasisagent.ingestion.base import IngestAdapter
from oasisagent.models import Event, EventMetadata, Severity

if TYPE_CHECKING:
    from oasisagent.config import N8nAdapterConfig
    from oasisagent.engine.queue import EventQueue

logger = logging.getLogger(__name__)


class N8nAdapter(IngestAdapter):
    """Polls N8N for service health and failed workflow executions.

    Uses state-based dedup for health status and time-based lookback
    for execution failures (only reports new failures since last poll).
    """

    def __init__(
        self, config: N8nAdapterConfig, queue: EventQueue,
    ) -> None:
        super().__init__(queue)
        self._config = config
        self._stopping = False
        self._task: asyncio.Task[None] | None = None
        self._connected = False

        # State tracker for health dedup
        self._service_ok: bool | None = None

        # Time-based lookback for executions
        self._last_execution_poll: datetime | None = None

    @property
    def name(self) -> str:
        return "n8n"

    def _headers(self) -> dict[str, str]:
        return {"X-N8N-API-KEY": self._config.api_key}

    async def start(self) -> None:
        self._task = asyncio.create_task(
            self._poll_loop(), name="n8n-poller",
        )
        await self._task

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def healthy(self) -> bool:
        return self._connected

    # -----------------------------------------------------------------
    # Poll loop
    # -----------------------------------------------------------------

    async def _poll_loop(self) -> None:
        timeout = aiohttp.ClientTimeout(total=self._config.timeout)
        backoff = self._config.poll_interval
        max_backoff = 300

        while not self._stopping:
            try:
                async with aiohttp.ClientSession(
                    headers=self._headers(), timeout=timeout,
                ) as session:
                    await self._poll_health(session)
                    if self._config.poll_executions:
                        await self._poll_executions(session)
                    self._connected = True
                    backoff = self._config.poll_interval  # reset on success
            except asyncio.CancelledError:
                return
            except (TimeoutError, aiohttp.ClientError) as exc:
                if self._connected:
                    logger.warning("N8N: connection error: %s", exc)
                self._connected = False
                backoff = min(backoff * 2, max_backoff)

                # Emit unreachable event on transition
                was_ok = self._service_ok
                self._service_ok = False
                if was_ok is not None and was_ok:
                    self._enqueue(Event(
                        source=self.name,
                        system="n8n",
                        event_type="n8n_unreachable",
                        entity_id="n8n",
                        severity=Severity.ERROR,
                        timestamp=datetime.now(tz=UTC),
                        payload={},
                        metadata=EventMetadata(
                            dedup_key="n8n:service:status",
                        ),
                    ))
            except Exception:
                self._connected = False
                logger.exception("N8N: unexpected error")
                backoff = min(backoff * 2, max_backoff)

            wait = self._config.poll_interval if self._connected else backoff
            for _ in range(wait):
                if self._stopping:
                    return
                await asyncio.sleep(1)

    # -----------------------------------------------------------------
    # Health check
    # -----------------------------------------------------------------

    async def _poll_health(self, session: aiohttp.ClientSession) -> None:
        """Poll the N8N API base to verify service reachability."""
        url = f"{self._config.url}/api/v1/workflows?limit=1"
        async with session.get(url) as resp:
            resp.raise_for_status()
            await resp.json(content_type=None)

        is_ok = True
        was_ok = self._service_ok
        self._service_ok = is_ok

        if was_ok is not None and not was_ok and is_ok:
            self._enqueue(Event(
                source=self.name,
                system="n8n",
                event_type="n8n_recovered",
                entity_id="n8n",
                severity=Severity.INFO,
                timestamp=datetime.now(tz=UTC),
                payload={},
                metadata=EventMetadata(
                    dedup_key="n8n:service:status",
                ),
            ))

    # -----------------------------------------------------------------
    # Execution failures
    # -----------------------------------------------------------------

    async def _poll_executions(self, session: aiohttp.ClientSession) -> None:
        """Poll /api/v1/executions for failed workflow executions.

        Uses time-based lookback: only reports executions that finished
        after the last poll timestamp.
        """
        now = datetime.now(tz=UTC)
        since = self._last_execution_poll
        self._last_execution_poll = now

        url = f"{self._config.url}/api/v1/executions"
        params: dict[str, str] = {
            "status": "error",
            "limit": "10",
        }
        async with session.get(url, params=params) as resp:
            resp.raise_for_status()
            data: dict[str, Any] = await resp.json(content_type=None)

        executions: list[dict[str, Any]] = data.get("data", [])

        for execution in executions:
            # Time-based filtering: skip executions from before last poll
            finished_at = execution.get("stoppedAt") or execution.get("finishedAt", "")
            if not finished_at:
                continue

            try:
                finished_ts = datetime.fromisoformat(
                    finished_at.replace("Z", "+00:00"),
                )
            except (ValueError, TypeError):
                continue

            # On first poll, only report recent failures (within poll_interval)
            if since is None:
                from datetime import timedelta
                lookback = timedelta(seconds=self._config.poll_interval)
                if finished_ts < now - lookback:
                    continue
            elif finished_ts <= since:
                continue

            exec_id = str(execution.get("id", ""))
            workflow_name = ""
            workflow_data = execution.get("workflowData") or {}
            if isinstance(workflow_data, dict):
                workflow_name = workflow_data.get("name", "")

            error_message = ""
            # N8N stores errors in lastNodeExecuted or in data
            last_node = execution.get("data", {})
            if isinstance(last_node, dict):
                result_data = last_node.get("resultData", {})
                if isinstance(result_data, dict):
                    error_msg = result_data.get("error", {})
                    if isinstance(error_msg, dict):
                        error_message = error_msg.get("message", "")
                    elif isinstance(error_msg, str):
                        error_message = error_msg

            self._enqueue(Event(
                source=self.name,
                system="n8n",
                event_type="n8n_execution_failed",
                entity_id=workflow_name or f"execution-{exec_id}",
                severity=Severity.WARNING,
                timestamp=datetime.now(tz=UTC),
                payload={
                    "execution_id": exec_id,
                    "workflow_name": workflow_name,
                    "error_message": error_message,
                    "finished_at": finished_at,
                },
                metadata=EventMetadata(
                    dedup_key=f"n8n:execution:{exec_id}",
                ),
            ))

    # -----------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------

    def _enqueue(self, event: Event) -> None:
        """Enqueue an event, logging on failure."""
        try:
            self._queue.put_nowait(event)
        except Exception:
            logger.warning(
                "N8N: failed to enqueue event: %s/%s",
                event.system, event.event_type,
            )
