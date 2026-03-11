"""Webhook receiver router — ``/ingest/webhook/{source}``.

Placeholder until issue #51 implements the full webhook ingestion adapter.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

router = APIRouter(tags=["ingestion"])


@router.post("/{source}")
async def receive_webhook(source: str) -> JSONResponse:
    """Accept webhook payloads. Not yet implemented — returns 501."""
    return JSONResponse(
        status_code=501,
        content={
            "error": "Webhook ingestion not yet implemented",
            "source": source,
            "hint": "See issue #51",
        },
    )
