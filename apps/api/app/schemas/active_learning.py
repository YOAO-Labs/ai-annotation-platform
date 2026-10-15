"""Explicitly bounded manager inputs for human-reviewed selection rounds."""

import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Selection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_version_id: uuid.UUID
    deployment_id: uuid.UUID
    sample_task_ids: list[uuid.UUID] = Field(min_length=2, max_length=32)
    seed: int = Field(ge=0, le=2_147_483_647, strict=True)
    batch_size: int = Field(ge=1, le=16, strict=True)
    max_inference_dispatches: int = Field(ge=1, le=96, strict=True)
    max_review_tasks: int = Field(ge=2, le=32, strict=True)
    confidence: float = Field(default=0.1, gt=0, le=1, allow_inf_nan=False)
    nms_iou: float = Field(default=0.7, gt=0, le=1, allow_inf_nan=False)

    @model_validator(mode="after")
    def budgets_and_unique_pool(self):
        if len(set(self.sample_task_ids)) != len(self.sample_task_ids):
            raise ValueError("候选任务不能重复")
        if self.max_review_tasks < 2 * self.batch_size:
            raise ValueError("审核预算须覆盖建议与随机对照两组")
        return self


class RoundCreate(Selection):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    request_id: uuid.UUID
    name: str = Field(min_length=1, max_length=120)
    preview_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class DecisionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    request_id: uuid.UUID
    action: Literal["accept", "reject"]
    ordinals: list[Annotated[int, Field(ge=0, lt=32, strict=True)]] = Field(
        min_length=1, max_length=32
    )
    batch_name: str = Field(default="主动学习人工复核", min_length=1, max_length=120)

    @model_validator(mode="after")
    def unique_members(self):
        if len(set(self.ordinals)) != len(self.ordinals) or any(
            type(n) is not int or not 0 <= n < 32 for n in self.ordinals
        ):
            raise ValueError("须选择不重复的有效候选序号")
        return self


class EvaluationReference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    evaluation_id: uuid.UUID
    attempt_id: uuid.UUID


class FeedbackCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: uuid.UUID
    evaluations: list[EvaluationReference] = Field(default_factory=list, max_length=4)

    @model_validator(mode="after")
    def unique_evaluations(self):
        if len(
            {(ref.evaluation_id, ref.attempt_id) for ref in self.evaluations}
        ) != len(self.evaluations):
            raise ValueError("后续评估引用不能重复")
        return self


class RoundOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    project_id: uuid.UUID
    name: str
    status: Literal[
        "queued", "running", "cancelling", "lost", "succeeded", "failed", "cancelled"
    ]
    spec: dict
    spec_digest: str
    inference_dispatches: int
    accepted_tasks: int
    current_attempt_id: uuid.UUID | None
    async_job_id: uuid.UUID | None
    error_code: str | None
    error_message: str | None
    created_at: datetime
    completed_at: datetime | None
    attempts: list[dict] = Field(default_factory=list)
