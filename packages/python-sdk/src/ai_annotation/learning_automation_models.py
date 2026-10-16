"""Request and response models for studies and active learning."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)

from ai_annotation.models import _AAPModel
from ai_annotation.training_models import TrainingRunCreate


class StudyBudgets(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    max_trials: int = Field(ge=1, le=20)
    max_planned_epochs: int = Field(ge=1, le=20_000)
    max_native_seconds: float = Field(gt=0, le=604_800)


class TrainingStudyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    base: TrainingRunCreate
    grid: dict[str, list[JsonValue]] = Field(min_length=1, max_length=10)
    objective: Literal[
        "metrics/mAP50-95",
        "metrics/mAP50",
        "metrics/mAP75",
        "metrics/precision",
        "metrics/recall",
        "metrics/AR100",
    ]
    direction: Literal["maximize"]
    budgets: StudyBudgets
    preview_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @field_validator("grid")
    @classmethod
    def bounded_values(
        cls, value: dict[str, list[JsonValue]]
    ) -> dict[str, list[JsonValue]]:
        if any(not 1 <= len(values) <= 20 for values in value.values()):
            raise ValueError("each grid parameter must contain 1 to 20 explicit values")
        return value

    @model_validator(mode="after")
    def full_configuration(self) -> TrainingStudyCreate:
        if self.base.configuration is None:
            raise ValueError("parameter search requires a complete configuration")
        if self.base.framework == "libreyolo" and self.objective in {
            "metrics/precision",
            "metrics/recall",
        }:
            raise ValueError("LibreYOLO evaluation does not report precision or recall")
        if self.base.framework == "ultralytics" and self.objective in {
            "metrics/mAP75",
            "metrics/AR100",
        }:
            raise ValueError("Ultralytics evaluation does not report mAP75 or AR100")
        return self


class ActiveLearningSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    model_version_id: UUID
    deployment_id: UUID
    sample_task_ids: list[UUID] = Field(min_length=2, max_length=32)
    seed: int = Field(ge=0, le=2_147_483_647, strict=True)
    batch_size: int = Field(ge=1, le=16, strict=True)
    max_inference_dispatches: int = Field(ge=1, le=96, strict=True)
    max_review_tasks: int = Field(ge=2, le=32, strict=True)
    confidence: float = Field(default=0.1, gt=0, le=1)
    nms_iou: float = Field(default=0.7, gt=0, le=1)

    @model_validator(mode="after")
    def budgets_and_unique_pool(self) -> ActiveLearningSelection:
        if len(set(self.sample_task_ids)) != len(self.sample_task_ids):
            raise ValueError("candidate task IDs must be unique")
        if self.max_review_tasks < 2 * self.batch_size:
            raise ValueError(
                "review budget must cover both proposed and control groups"
            )
        return self


class ActiveLearningRoundCreate(ActiveLearningSelection):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    request_id: UUID
    name: str = Field(min_length=1, max_length=120)
    preview_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class ActiveLearningDecisionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    request_id: UUID
    action: Literal["accept", "reject"]
    ordinals: list[Annotated[int, Field(ge=0, lt=32, strict=True)]] = Field(
        min_length=1, max_length=32
    )
    batch_name: str = Field(default="主动学习人工复核", min_length=1, max_length=120)

    @model_validator(mode="after")
    def unique_ordinals(self) -> ActiveLearningDecisionCreate:
        if len(set(self.ordinals)) != len(self.ordinals):
            raise ValueError("candidate ordinals must be unique")
        return self


class ActiveLearningEvaluationReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evaluation_id: UUID
    attempt_id: UUID


class ActiveLearningFeedbackCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: UUID
    evaluations: list[ActiveLearningEvaluationReference] = Field(
        default_factory=list, max_length=4
    )

    @model_validator(mode="after")
    def unique_evaluations(self) -> ActiveLearningFeedbackCreate:
        refs = {(row.evaluation_id, row.attempt_id) for row in self.evaluations}
        if len(refs) != len(self.evaluations):
            raise ValueError("evaluation references must be unique")
        return self


class TrainingStudyPreview(_AAPModel):
    plan: dict[str, Any]
    preview_digest: str


class TrainingStudyListItem(_AAPModel):
    id: UUID
    name: str
    status: str
    scheduling: bool
    message: str | None
    created_at: datetime
    trial_count: int


class TrainingStudyPage(_AAPModel):
    items: list[TrainingStudyListItem]
    total: int


class TrainingStudyTrial(_AAPModel):
    id: UUID
    ordinal: int
    values: dict[str, JsonValue]
    config_digest: str
    planned_epochs: int
    run_id: UUID | None
    status: str
    result: dict[str, Any] | None
    result_error: str | None
    actual_epochs: int | None
    completion: str | None
    queue_seconds: float | None
    lease_seconds: float | None
    native_seconds: float
    native_time_unknown: bool


class TrainingStudyDetail(_AAPModel):
    id: UUID
    project_id: UUID
    name: str
    status: str
    scheduling: bool
    message: str | None
    plan_digest: str
    plan: dict[str, Any]
    accounting: dict[str, Any]
    trials: list[TrainingStudyTrial]
    rankings: list[dict[str, Any]]
    created_at: datetime


class ActiveLearningPoolItem(_AAPModel):
    task_id: UUID
    display_id: str
    file_name: str
    dataset_item_id: UUID | None
    scene_id: UUID | None


class ActiveLearningPoolPage(_AAPModel):
    items: list[ActiveLearningPoolItem]
    total: int
    limit: int
    offset: int


class ActiveLearningPreview(_AAPModel):
    preview_digest: str
    pool_size: int
    candidate_count: int
    omitted: list[dict[str, Any]]
    samples: list[dict[str, Any]]
    model_ref: dict[str, Any]
    profile: dict[str, Any]
    strategy: dict[str, Any]
    exclusion_reference_count: int
    batch_size: int
    max_inference_dispatches: int
    max_review_tasks: int
    claim: str


class ActiveLearningAttempt(_AAPModel):
    id: UUID
    ordinal: int
    status: str
    heartbeat_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    error_code: str | None
    error_message: str | None
    has_report: bool


class ActiveLearningRound(_AAPModel):
    id: UUID
    project_id: UUID
    name: str
    status: Literal[
        "queued", "running", "cancelling", "lost", "succeeded", "failed", "cancelled"
    ]
    spec: dict[str, Any]
    spec_digest: str
    inference_dispatches: int
    accepted_tasks: int
    current_attempt_id: UUID | None
    async_job_id: UUID | None
    error_code: str | None
    error_message: str | None
    created_at: datetime
    completed_at: datetime | None
    attempts: list[ActiveLearningAttempt] = Field(default_factory=list)


class ActiveLearningRoundPage(_AAPModel):
    items: list[ActiveLearningRound]
    total: int


class ActiveLearningCandidate(_AAPModel):
    ordinal: int
    task_id: UUID
    status: Literal["pending", "inflight", "succeeded", "failed"]
    disposition: Literal["accepted", "rejected"] | None
    decision_id: UUID | None
    has_image: bool
    error_code: str | None
    evidence: dict[str, Any] | None


class ActiveLearningCandidateList(_AAPModel):
    items: list[ActiveLearningCandidate]


class ActiveLearningReport(_AAPModel):
    schema_version: int = Field(alias="schema")
    round_id: UUID
    attempt_id: UUID
    spec_digest: str
    strategy: dict[str, Any]
    ranking: list[dict[str, Any]]
    missing_signal_count: int
    inference_dispatches: int
    max_review_tasks: int
    claim: str
    report_digest: str


class ActiveLearningDecision(_AAPModel):
    id: UUID
    request_id: UUID
    action: Literal["accept", "reject"]
    accepted_batch_id: UUID | None
    snapshot: dict[str, Any]
    snapshot_digest: str
    created_at: datetime


class ActiveLearningDecisionList(_AAPModel):
    items: list[ActiveLearningDecision]


class ActiveLearningFeedback(_AAPModel):
    id: UUID
    request_id: UUID
    snapshot: dict[str, Any]
    snapshot_digest: str
    created_at: datetime


class ActiveLearningFeedbackList(_AAPModel):
    items: list[ActiveLearningFeedback]
