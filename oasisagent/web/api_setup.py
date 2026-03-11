"""First-run setup wizard API.

When no admin user exists in the database, the agent is in "setup mode".
These endpoints guide the operator through initial configuration:

1. ``POST /api/v1/setup/admin`` — Create admin account
2. ``POST /api/v1/setup/core-services`` — Configure MQTT + InfluxDB
3. ``POST /api/v1/setup/complete`` — Mark setup as done

All non-setup, non-health endpoints return 403 until setup is complete.
The UI for this wizard ships in v0.3.1; this is the backend API only.
"""

from __future__ import annotations

from typing import Any

import bcrypt
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from oasisagent.db.config_store import ConfigStore  # noqa: TC001 — used at runtime

router = APIRouter(prefix="/setup", tags=["setup"])


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class AdminCreate(BaseModel):
    """Request body for creating the admin account."""

    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=8, max_length=200)
    totp_secret: str | None = None


class CoreServicesSetup(BaseModel):
    """Request body for configuring core services (MQTT + InfluxDB)."""

    model_config = ConfigDict(extra="forbid")

    mqtt_broker: str = Field(default="mqtt://localhost:1883")
    mqtt_username: str = ""
    mqtt_password: str = ""
    influxdb_url: str = Field(default="http://localhost:8086")
    influxdb_token: str = ""
    influxdb_org: str = "myorg"
    influxdb_bucket: str = "oasisagent"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_store(request: Request) -> ConfigStore:
    return request.app.state.config_store


async def _require_setup_mode(request: Request) -> None:
    """Raise 409 if setup is already complete (admin exists)."""
    store = _get_store(request)
    if await store.has_admin():
        raise HTTPException(
            status_code=409,
            detail="Setup already complete. Admin user exists.",
        )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/status")
async def setup_status(request: Request) -> dict[str, Any]:
    """Check whether the agent is in setup mode.

    Returns:
        ``{"setup_required": true}`` if no admin exists,
        ``{"setup_required": false}`` if setup is complete.
    """
    store = _get_store(request)
    needs_setup = not await store.has_admin()
    return {"setup_required": needs_setup}


@router.post("/admin", status_code=201)
async def create_admin(request: Request, body: AdminCreate) -> dict[str, Any]:
    """Step 1: Create the admin account.

    Only allowed when no admin user exists. Hashes the password with
    bcrypt before storing.
    """
    await _require_setup_mode(request)
    store = _get_store(request)

    password_hash = bcrypt.hashpw(
        body.password.encode(), bcrypt.gensalt(),
    ).decode()
    user_id = await store.create_user(
        username=body.username,
        password_hash=password_hash,
        is_admin=True,
        totp_secret=body.totp_secret,
    )

    return {
        "id": user_id,
        "username": body.username,
        "is_admin": True,
        "message": "Admin account created. Proceed to core services setup.",
    }


@router.post("/core-services", status_code=201)
async def setup_core_services(
    request: Request, body: CoreServicesSetup,
) -> dict[str, Any]:
    """Step 2: Configure MQTT broker and InfluxDB.

    Creates connector and service rows in the database. Can be called
    multiple times — uses the fixed names ``mqtt-primary`` and
    ``influxdb-primary`` with upsert-like behavior (deletes existing
    before creating).

    Note: intentionally does NOT call ``_require_setup_mode``.
    Re-configuring core services post-setup is a valid operation
    (e.g. operator changes broker address). The setup guard middleware
    already controls access during setup mode.
    """
    store = _get_store(request)

    async with store.transaction():
        # MQTT connector
        existing_connectors = await store.list_connectors()
        for c in existing_connectors:
            if c["name"] == "mqtt-primary":
                await store.delete_connector(c["id"])

        mqtt_config: dict[str, Any] = {
            "broker": body.mqtt_broker,
            "username": body.mqtt_username,
            "password": body.mqtt_password,
        }
        mqtt_id = await store.create_connector("mqtt", "mqtt-primary", mqtt_config)

        # InfluxDB service
        existing_services = await store.list_services()
        for s in existing_services:
            if s["name"] == "influxdb-primary":
                await store.delete_service(s["id"])

        influx_config: dict[str, Any] = {
            "url": body.influxdb_url,
            "token": body.influxdb_token,
            "org": body.influxdb_org,
            "bucket": body.influxdb_bucket,
        }
        influx_id = await store.create_service("influxdb", "influxdb-primary", influx_config)

    return {
        "mqtt_connector_id": mqtt_id,
        "influxdb_service_id": influx_id,
        "message": "Core services configured. Call /api/v1/setup/complete to finish.",
    }


@router.post("/complete")
async def complete_setup(request: Request) -> dict[str, Any]:
    """Step 3: Mark setup as complete.

    Verifies that an admin user exists. Returns success if so.
    The agent will begin normal operation on the next request cycle.
    """
    store = _get_store(request)

    if not await store.has_admin():
        raise HTTPException(
            status_code=400,
            detail="Cannot complete setup: no admin user exists. "
            "Call POST /api/v1/setup/admin first.",
        )

    return {
        "status": "complete",
        "message": "Setup complete. OasisAgent is ready.",
    }
