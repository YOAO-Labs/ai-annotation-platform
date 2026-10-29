"""Typed models for platform administration and preannotation operations."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from ai_annotation.models import _SDKModel


class _RequestModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        allow_inf_nan=False,
        protected_namespaces=("model_validate", "model_dump"),
    )


class PreannotateQueueItem(_SDKModel):
    batch_id: UUID
    batch_name: str
    batch_status: str
    project_id: UUID
    project_name: str
    project_display_id: str | None = None
    total_tasks: int
    prediction_count: int
    failed_count: int
    last_run_at: datetime | None = None
    can_retry: bool


class PreannotateQueueResponse(_SDKModel):
    items: list[PreannotateQueueItem]


PreannotateClearMode = Literal["predictions_only", "reset_to_draft"]


class PreannotateBulkClearRequest(_RequestModel):
    batch_ids: list[UUID] = Field(min_length=1, max_length=50)
    mode: PreannotateClearMode = "predictions_only"
    reason: str = Field(min_length=10, max_length=500)


class PreannotateBulkClearItem(_SDKModel):
    batch_id: UUID
    reason: str


class PreannotateBulkClearResponse(_SDKModel):
    succeeded: list[UUID] = Field(default_factory=list)
    skipped: list[PreannotateBulkClearItem] = Field(default_factory=list)
    failed: list[PreannotateBulkClearItem] = Field(default_factory=list)


class PreannotateProjectSummary(_SDKModel):
    project_id: UUID
    project_name: str
    project_display_id: str | None = None
    type_key: str
    data_type: str = "image"
    ml_backend_id: UUID | None = None
    ml_backend_name: str | None = None
    ml_backend_state: str | None = None
    ml_backend_max_concurrency: int | None = None
    ready_batches: int = 0
    active_batches: int = 0
    last_job_at: datetime | None = None
    recent_failures: int = 0


class PreannotateProjectSummaryResponse(_SDKModel):
    items: list[PreannotateProjectSummary]


HealthStatus = Literal["ok", "degraded", "down"]


class HealthComponent(_SDKModel):
    name: str
    label: str
    status: HealthStatus
    latency_ms: float | None = None
    detail: str | None = None


class CeleryWorkerHealth(_SDKModel):
    name: str
    last_heartbeat_seconds_ago: float | None = None
    pool_max: int | None = None
    status: HealthStatus


class CeleryQueueHealth(_SDKModel):
    name: str
    length: int
    status: HealthStatus


class CeleryHealthSummary(_SDKModel):
    active_count: int
    workers: list[CeleryWorkerHealth]
    queues: list[CeleryQueueHealth]


class SystemHealthResponse(_SDKModel):
    status: HealthStatus
    version: str
    components: list[HealthComponent]
    celery: CeleryHealthSummary


AnalyticsPanelName = Literal[
    "throughput_daily",
    "reject_rate_by_type",
    "duration_dist",
    "activity_heatmap",
]


class AnalyticsPanelResponse(_SDKModel):
    panel: AnalyticsPanelName
    data: Any


class AliasFrequencyResponse(_SDKModel):
    project_id: UUID
    total_predictions: int
    frequency: dict[str, int]
    last_computed_at: datetime
