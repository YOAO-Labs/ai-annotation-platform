"""Public root health JSON, including degraded responses."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from ai_annotation.models import _SDKModel


class HealthQueue(_SDKModel):
    name: str
    length: int


class HealthWorker(_SDKModel):
    name: str
    last_heartbeat_seconds_ago: float | None = None
    pool_max: int | None = None


class HealthCheck(_SDKModel):
    status: Literal["ok", "error"]
    latency_ms: float | None = None
    detail: str | None = None
    active_count: int | None = None
    workers: list[HealthWorker] = Field(default_factory=list)
    queues: list[HealthQueue] = Field(default_factory=list)


class HealthReport(_SDKModel):
    status: Literal["ok", "degraded"]
    version: str
    checks: dict[str, HealthCheck]
