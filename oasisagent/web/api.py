"""REST API router — ``/api/v1/``."""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Request

router = APIRouter(tags=["api"])


@router.get("/status")
async def status(request: Request) -> dict[str, Any]:
    """Agent status — uptime, events processed, queue depth."""
    orchestrator = request.app.state.orchestrator
    start_time: float = getattr(request.app.state, "start_time", 0.0)
    uptime = time.monotonic() - start_time if start_time else 0.0

    queue_depth = 0
    if orchestrator._queue is not None:
        queue_depth = orchestrator._queue.size

    return {
        "status": "running",
        "uptime_seconds": round(uptime, 1),
        "events_processed": orchestrator._events_processed,
        "actions_taken": orchestrator._actions_taken,
        "errors": orchestrator._errors,
        "queue_depth": queue_depth,
    }
