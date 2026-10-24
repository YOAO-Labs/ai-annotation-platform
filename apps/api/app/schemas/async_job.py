"""v0.10.16 · async_jobs API schema（ROADMAP §1.7）。"""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator


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
                "prediction_authority",
                "repair_authority",
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
