"""Typed public contracts for controlled models, policies, and evaluations."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator

from ai_annotation.models import _AAPModel
from ai_annotation.training_models import (
    TrainingConfiguration,
    TrainingInitialization,
    TrainingOrigin,
    TrainingParameters,
)

Disposition = Literal["active", "research", "declined", "archived"]
NodeKind = Literal[
    "dataset",
    "run",
    "attempt",
    "model",
    "deployment",
    "release",
    "recipe",
    "recipe_revision",
    "evaluation",
]
EvaluationStatus = Literal[
    "queued", "running", "cancelling", "lost", "succeeded", "failed", "cancelled"
]


class TrainedModelCreateRequest(_AAPModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    request_id: UUID
    name: str = Field(min_length=1, max_length=120)
    run_id: UUID
    attempt_id: UUID
    bindings: list[str] = Field(min_length=1, max_length=1000)


class ModelDeploymentCreateRequest(_AAPModel):
    model_config = ConfigDict(extra="forbid")

    registry_id: UUID


class ModelReleaseCreateRequest(_AAPModel):
    model_config = ConfigDict(extra="forbid")

    request_id: UUID
    expected_generation: int = Field(ge=0)
    model_version_id: UUID | None = None
    deployment_id: UUID | None = None
    rollback_from_id: UUID | None = None
    sample_confirmed: bool = False

    @model_validator(mode="after")
    def require_model_deployment_pair(self):
        if (self.model_version_id is None) != (self.deployment_id is None):
            raise ValueError(
                "model_version_id and deployment_id must be provided together"
            )
        if self.model_version_id is not None and not self.sample_confirmed:
            raise ValueError("sample_confirmed must be true when publishing a model")
        return self


class ModelDecisionCreateRequest(_AAPModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    request_id: UUID
    disposition: Disposition
    note: str = Field(min_length=1, max_length=2000)


class LearningPolicySaveRequest(_AAPModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=0)
    enabled: bool = False
    parent_id: UUID
    batch_id: UUID | None = None
    framework: Literal["ultralytics", "libreyolo"] = "ultralytics"
    base_model: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    parameters: TrainingParameters = Field(default_factory=TrainingParameters)
    configuration: TrainingConfiguration | None = None
    initialization: TrainingInitialization | None = None
    origin: TrainingOrigin | None = None
    min_changed_samples: int = Field(ge=1, le=20000)
    check_interval_minutes: int = Field(ge=1, le=1440)
    cooldown_minutes: int = Field(ge=0, le=10080)

    @model_validator(mode="after")
    def validate_configuration_sources(self):
        if self.configuration is not None and "parameters" in self.model_fields_set:
            raise ValueError(
                "configuration and legacy parameters cannot be sent together"
            )
        if self.origin is not None and (
            self.origin.kind != "recipe" or self.configuration is None
        ):
            raise ValueError("recipe origin requires a complete configuration")
        if (
            self.initialization is not None
            and self.initialization.kind == "trained_model"
            and self.configuration is None
        ):
            raise ValueError(
                "trained-model initialization requires a complete configuration"
            )
        return self


class SampleEvaluationCreateRequest(_AAPModel):
    model_config = ConfigDict(extra="forbid")

    request_id: UUID
    name: str = Field(min_length=1, max_length=120)
    model_version_id: UUID
    deployment_id: UUID
    dataset_id: UUID
    sample_task_ids: list[UUID] = Field(min_length=1, max_length=32)
    role: Literal["validation_selection", "held_out_test"] = "validation_selection"
    held_out_confirmed: bool = Field(default=False, strict=True)
    confidence: float = Field(
        default=0.25, ge=0, le=1, allow_inf_nan=False, strict=True
    )
    matching_iou: float = Field(
        default=0.5, ge=0, le=1, allow_inf_nan=False, strict=True
    )
    nms_iou: float = Field(default=0.7, ge=0, le=1, allow_inf_nan=False, strict=True)

    @model_validator(mode="after")
    def validate_sample_selection(self):
        if len(set(self.sample_task_ids)) != len(self.sample_task_ids):
            raise ValueError("sample_task_ids must not contain duplicates")
        if self.role == "held_out_test" and not self.held_out_confirmed:
            raise ValueError("held_out_test requires held_out_confirmed=true")
        if not self.name.strip():
            raise ValueError("name must not be blank")
        self.name = self.name.strip()
        return self


class TrainedModel(_AAPModel):
    id: UUID
    project_id: UUID
    run_id: UUID
    attempt_id: UUID
    name: str
    framework: str
    framework_version: str
    artifact: dict[str, Any]
    classes: list[str]
    bindings: list[str]
    binding_digest: str
    provenance: dict[str, Any]
    evaluation: dict[str, Any]
    created_at: datetime
    task: str = "detect"
    media_type: str = "image"
    disposition: Disposition | None = None
    decision_sequence: int = 0
    archived: bool = False
    is_current_release: bool = False


class TrainedModelPage(_AAPModel):
    items: list[TrainedModel]
    total: int


class ModelDeployment(_AAPModel):
    id: UUID
    model_version_id: UUID
    registry_id: UUID
    status: str
    receipt: dict[str, Any]
    trial: dict[str, Any]
    error_message: str | None
    checked_at: datetime | None
    created_at: datetime


class TrainedModelDetail(_AAPModel):
    model: TrainedModel
    deployments: list[ModelDeployment]


class ModelDownload(_AAPModel):
    url: str
    sha256: str
    size_bytes: int


class ModelRelease(_AAPModel):
    id: UUID
    project_id: UUID
    model_version_id: UUID | None
    deployment_id: UUID | None
    previous_release_id: UUID | None
    rollback_from_id: UUID | None
    generation: int
    snapshot: dict[str, Any]
    created_at: datetime


class ModelReleasePage(_AAPModel):
    current: ModelRelease | None
    generation: int
    current_release_id: UUID | None
    total: int
    items: list[ModelRelease]


class ModelDecision(_AAPModel):
    id: UUID
    project_id: UUID
    model_version_id: UUID
    sequence: int
    creator_id: UUID | None
    creator_label: str
    disposition: Disposition
    note: str
    created_at: datetime


class ModelDecisionPage(_AAPModel):
    items: list[ModelDecision]
    next_before: int | None = None


class LineageNode(_AAPModel):
    kind: NodeKind
    id: UUID
    name: str
    status: str | None = None
    created_at: datetime
    facts: dict[str, str] = Field(default_factory=dict)


class LineageLink(_AAPModel):
    relation: str
    target: LineageNode | None
    gap: str | None = None


class LineageUsage(_AAPModel):
    retained_count: int | None
    unavailable_reason: str | None = None
    truncated: bool
    limit: int = 1000
    scope: str = "retained_attributable_predictions"


class ModelLineage(_AAPModel):
    node: LineageNode
    inputs: list[LineageLink]
    sections: list[str]
    relation: str | None = None
    items: list[LineageNode] = Field(default_factory=list)
    next_cursor: str | None = None
    usage: LineageUsage | None = None


class LearningPolicy(_AAPModel):
    id: UUID
    project_id: UUID
    creator_id: UUID | None
    revision: int
    enabled: bool
    config: dict[str, Any]
    initialization_model_id: UUID | None = None
    baseline_dataset_id: UUID | None
    blocked_train_digest: str | None
    last_checked_at: datetime | None
    next_check_at: datetime | None
    last_started_at: datetime | None
    state: str
    message: str | None


class LearningTrigger(_AAPModel):
    id: UUID
    policy_revision: int
    dataset_id: UUID
    status: str
    candidate_digest: str | None
    train_digest: str | None
    validation_digest: str | None
    changed_samples: int
    message: str | None
    created_at: datetime
    completed_at: datetime | None
    run_id: UUID | None = None


class LearningTriggerPage(_AAPModel):
    total: int
    items: list[LearningTrigger]


class EvaluationProfile(_AAPModel):
    schema_version: Literal[1] = Field(alias="schema")
    implementation: Literal["aap-ultralytics-detection"]
    revision: Literal[1]
    ultralytics_version: str
    torch_version: str
    pillow_version: str
    source_digest: str
    storage_identity: UUID
    effective_device: str
    options: dict[str, Any]
    profile_digest: str

    model_config = ConfigDict(extra="allow", populate_by_name=True)


class FrozenEvaluationSample(_AAPModel):
    ordinal: int
    task_id: UUID
    sha256: str
    label_sha256: str
    width: int
    height: int
    size_bytes: int
    split: Literal["val"]
    explicit_negative: bool | None
    truth: list[dict[str, Any]]


class EvaluationSpec(_AAPModel):
    schema_version: Literal[1] = Field(alias="schema")
    evaluation_id: UUID
    model_ref: dict[str, Any]
    dataset_id: UUID
    snapshot_digest: str
    manifest_digest: str
    package_sha256: str
    benchmark_digest: str
    comparison_digest: str
    classes: list[str]
    native_to_benchmark: list[int]
    role: Literal["validation_selection", "held_out_test"]
    held_out_confirmed: bool
    confidence: float
    matching_iou: float
    nms_iou: float
    evaluator: dict[str, Any]
    preprocessing: dict[str, Any]
    profile: EvaluationProfile
    profile_digest: str
    samples: list[FrozenEvaluationSample]
    issued_at: int
    expires_at: int

    model_config = ConfigDict(extra="allow", populate_by_name=True)


class EvaluationSampleResult(_AAPModel):
    ordinal: int
    task_id: UUID
    sha256: str
    label_sha256: str
    width: int
    height: int
    status: Literal["pending", "succeeded", "failed"]
    truth: list[dict[str, Any]]
    predictions: list[dict[str, Any]] = Field(default_factory=list)
    matching: dict[str, Any] | None = None
    derived_sha256: str | None = None
    preprocessing: dict[str, Any] | None = None
    request_id: UUID
    binding_digest: str | None = None
    inference: dict[str, Any] | None = None
    produced_attempt_id: UUID | None = None
    error_code: str | None = None
    error_message: str | None = None
    evidence_digest: str | None = None


class EvaluationQualification(_AAPModel):
    model_ref: dict[str, Any]
    profile: EvaluationProfile
    evaluator: dict[str, Any]
    preprocessing: dict[str, Any]
    limits: dict[str, int]


class SampleEvaluationAttempt(_AAPModel):
    id: UUID
    ordinal: int
    generation: int
    status: EvaluationStatus
    heartbeat_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    error_code: str | None
    error_message: str | None
    report: dict[str, Any] | None


class SampleEvaluation(_AAPModel):
    id: UUID
    project_id: UUID
    name: str
    model_version_id: UUID
    deployment_id: UUID
    dataset_id: UUID
    request_id: UUID
    status: EvaluationStatus
    spec_digest: str
    benchmark_digest: str
    comparison_digest: str
    spec: EvaluationSpec
    current_attempt_id: UUID | None
    async_job_id: UUID | None
    progress: dict[str, int]
    error_code: str | None
    error_message: str | None
    attempts: list[SampleEvaluationAttempt]
    created_at: datetime
    completed_at: datetime | None
    deletion_requested_at: datetime | None


class SampleEvaluationPage(_AAPModel):
    items: list[SampleEvaluation]
    total: int


class EvaluationReport(_AAPModel):
    schema_version: Literal[1] = Field(alias="schema")
    evaluation_id: UUID
    attempt_id: UUID
    spec_digest: str
    benchmark_digest: str
    comparison_digest: str
    model_ref: dict[str, Any]
    evaluator: dict[str, Any]
    status: Literal["succeeded", "failed", "cancelled"]
    complete: bool
    total_samples: int
    succeeded_samples: int
    failed_samples: int
    pending_samples: int
    aggregate: dict[str, Any] | None
    samples: list[EvaluationSampleResult]

    model_config = ConfigDict(extra="allow", populate_by_name=True)


class EvaluationSampleResultPage(_AAPModel):
    evaluation_id: UUID
    attempt_id: UUID
    report_sha256: str
    spec_digest: str
    total: int
    offset: int
    limit: int
    items: list[EvaluationSampleResult]


class EvaluationSource(_AAPModel):
    evaluation_id: UUID
    attempt_id: UUID
    ordinal: int
    checked_at: datetime
    source: Literal["found", "deleted", "unavailable"]
    task_id: UUID | None
    project_id: UUID | None
    batch_id: UUID | None
    media: Literal["same", "changed", "unavailable"]
    labels: Literal["same", "changed", "unsupported", "unavailable"]
    current_sha256: str | None
    current_label_sha256: str | None
    current_width: int | None
    current_height: int | None
    task_status: str | None
    reviewed: bool | None
    negative_confirmed: bool | None
    reason: str | None


class EvaluationComparisonCandidate(_AAPModel):
    evaluation_id: UUID
    name: str
    status: EvaluationStatus
    attempt_id: UUID
    model_ref: dict[str, Any]
    spec_digest: str
    comparison_digest: str
    aggregate: dict[str, Any] | None


class EvaluationComparison(_AAPModel):
    compatible: bool
    reasons: list[str]
    candidates: list[EvaluationComparisonCandidate]
    delta_right_minus_left: dict[str, int | float | None] | None
    sample_pairs: list[dict[str, int]]
