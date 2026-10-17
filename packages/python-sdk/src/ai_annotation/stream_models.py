"""Additive event contracts for the six public WebSocket channels."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import Field, field_validator

from ai_annotation.models import MLBackendStatsSnapshot, _AAPModel
from ai_annotation.notification_models import Notification


class NotificationSyncEvent(_AAPModel):
    type: Literal["notifications.sync"]
    reason: str | None = None


NotificationStreamEvent = Notification | NotificationSyncEvent


class PreannotationProgressEvent(_AAPModel):
    current: int
    total: int
    status: str
    error: str | None = None
    pipeline_stages: list[dict[str, Any]] | None = None


class PredictionJobEvent(PreannotationProgressEvent):
    project_id: UUID
    project_name: str | None = None
    job_id: UUID | None = None
    batch_id: UUID | None = None
    job_celery_task_id: str | None = None


class BatchEvent(_AAPModel):
    type: str
    at: datetime
    batch_id: UUID | None = None
    batch_ids: list[UUID] = Field(default_factory=list)
    from_status: str | None = Field(default=None, alias="from")
    to_status: str | None = Field(default=None, alias="to")


class VideoTrackerEvent(_AAPModel):
    type: str
    job_id: UUID
    task_id: UUID
    at: datetime
    dataset_item_id: UUID | None = None
    annotation_id: UUID | None = None
    current: int | None = None
    total: int | None = None
    frame_index: int | None = None
    geometry: dict[str, Any] | None = None
    confidence: float | None = None
    outside: bool | None = None
    source: str | None = None
    error: str | None = None

    @field_validator("annotation_id", mode="before")
    @classmethod
    def legacy_absent_annotation(cls, value):
        return None if value == "None" else value


class MLStatsEvent(_AAPModel):
    backends: list[MLBackendStatsSnapshot]
    timestamp: datetime


__all__ = [
    "NotificationSyncEvent",
    "NotificationStreamEvent",
    "PreannotationProgressEvent",
    "PredictionJobEvent",
    "BatchEvent",
    "VideoTrackerEvent",
    "MLStatsEvent",
]
