"""Pydantic request/response models for the config CRUD REST API."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class RowCreate(BaseModel):
    """Request body for creating a connector, service, or notification channel."""

    model_config = ConfigDict(extra="forbid")

    type: str
    name: str
    enabled: bool = True
    config: dict[str, Any]


class RowUpdate(BaseModel):
    """Request body for updating a connector, service, or notification channel."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    enabled: bool | None = None
    config: dict[str, Any] | None = None


class RowResponse(BaseModel):
    """Response body for a single connector, service, or notification channel."""

    model_config = ConfigDict(extra="forbid")

    id: int
    type: str
    name: str
    enabled: bool
    config: dict[str, Any]
    created_at: str
    updated_at: str
