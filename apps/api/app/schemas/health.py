"""Public health reports share a schema for ready and degraded responses."""

from pydantic import BaseModel, ConfigDict, Field


class HealthQueue(BaseModel):
    name: str
    length: int


class HealthWorker(BaseModel):
    name: str
    last_heartbeat_seconds_ago: float | None = None
    pool_max: int | None = None


class HealthCheck(BaseModel):
    model_config = ConfigDict(extra="allow")

    status: str
    latency_ms: float | None = None
    detail: str | None = None
    active_count: int | None = None
    workers: list[HealthWorker] = Field(default_factory=list)
    queues: list[HealthQueue] = Field(default_factory=list)


class HealthReport(BaseModel):
    status: str
    version: str
    checks: dict[str, HealthCheck]
