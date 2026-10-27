"""v0.10.16 · async_jobs API schema（ROADMAP §1.7）。"""

import uuid
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.services import storage_job_progress as progress


class StorageJobProgressOut(BaseModel):
    schema_version: Literal[1] = Field(alias="schema")
    execution_id: uuid.UUID
    stage: progress.Stage
    unit: progress.Unit
    completed: int = Field(ge=0)
    sequence: int = Field(gt=0)
    observed_at: datetime
    idle_seconds: int | None = Field(default=None, ge=0)
    attention_after_seconds: Literal[900] = 900


class AsyncJobOut(BaseModel):
    """v0.10.16 · 异步任务行 schema（前端铃铛 + 详情页消费）。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: str
    project_id: uuid.UUID | None
    user_id: uuid.UUID | None
    status: Literal["pending", "running", "completed", "failed", "cancelled"]
    progress_pct: int
    payload: dict
    result: dict
    error_message: str | None
    project_display_id: str | None = None
    project_name: str | None = None
    celery_task_id: str | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime
    last_progress_at: datetime | None = None
    progress_observation: StorageJobProgressOut | None = None

    @model_validator(mode="before")
    @classmethod
    def public_progress(cls, value):
        if isinstance(value, dict):
            data = dict(value)
        else:
            data = {
                key: getattr(value, key)
                for key in cls.model_fields
                if hasattr(value, key)
            }
        last_at, observation = progress.public(
            data.get("id"),
            data.get("kind"),
            data.get("celery_task_id"),
            data.get("last_progress_at"),
            data.get("progress_observation"),
            observed_at=datetime.now(timezone.utc),
        )
        data["last_progress_at"], data["progress_observation"] = last_at, observation
        return data

    @field_validator("payload")
    @classmethod
    def public_payload(cls, payload):
        return {
            key: value
            for key, value in payload.items()
            if key
            not in {
                "directory_authority",
                "export_authority",
                "output_authority",
                "lidar_media",
                "image_media",
                "video_media",
                "prediction_authority",
                "video_prediction_authority",
                "repair_authority",
                "point_cloud_quality_authority",
            }
        }


class AsyncJobListResponse(BaseModel):
    items: list[AsyncJobOut]
    total: int


class AsyncJobRetryFailedResponse(BaseModel):
    status: Literal["queued"]
    job_id: uuid.UUID
    queued: int
    skipped: int


class AsyncJobCancelResponse(BaseModel):
    id: uuid.UUID
    status: Literal["cancelled", "cancel_requested"]
