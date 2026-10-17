"""Public root health JSON, including degraded responses."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from ai_annotation.models import _AAPModel


class HealthQueue(_AAPModel):
    name: str
    length: int


class HealthWorker(_AAPModel):
    name: str
    last_heartbeat_seconds_ago: float | None = None
    pool_max: int | None = None


class HealthCheck(_AAPModel):
    status: Literal["ok", "error"]
    latency_ms: float | None = None
    detail: str | None = None
    active_count: int | None = None
    workers: list[HealthWorker] = Field(default_factory=list)
    queues: list[HealthQueue] = Field(default_factory=list)


class HealthReport(_AAPModel):
    status: Literal["ok", "degraded"]
    version: str
    checks: dict[str, HealthCheck]
