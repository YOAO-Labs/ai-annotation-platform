"""Typed public contracts for the project's frozen training workflows."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from ai_annotation.models import _AAPModel


class _RequestModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        allow_inf_nan=False,
        protected_namespaces=("model_validate", "model_dump"),
    )


class TrainingDatasetSelection(_RequestModel):
    task: Literal["detect", "segment"] = "detect"
    batch_id: uuid.UUID | None = None
    task_ids: list[uuid.UUID] = Field(default_factory=list, max_length=20_000)
    mode: Literal["reviewed", "manager_confirmed"] = "reviewed"
    confirmed_negative_ids: list[uuid.UUID] = Field(
        default_factory=list, max_length=20_000
    )
    seed: int = Field(default=42, ge=0, le=2_147_483_647)
    validation_fraction: float = Field(default=0.2, ge=0.05, le=0.5)
    parent_id: uuid.UUID | None = Field(
        default=None,
        description="Parent frozen training-version UUID for split inheritance, not a task-stream ID or ordinal.",
    )

    @model_validator(mode="after")
    def explicit_confirmation(self):
        if self.mode == "manager_confirmed" and not self.task_ids:
            raise ValueError("manager_confirmed mode requires explicit task_ids")
        if self.confirmed_negative_ids and (
            self.mode != "manager_confirmed"
            or not set(self.confirmed_negative_ids) <= set(self.task_ids)
        ):
            raise ValueError(
                "confirmed negatives must be selected manager-confirmed tasks"
            )
        if len(set(self.task_ids)) != len(self.task_ids):
            raise ValueError("task_ids must not contain duplicates")
        return self


class TrainingDatasetCreate(TrainingDatasetSelection):
    name: str = Field(min_length=1, max_length=120)
    request_id: uuid.UUID = Field(
        description="Caller-generated idempotency UUID; preserve it and the exact payload on an uncertain retry."
    )
    preflight_digest: str = Field(
        pattern=r"^[0-9a-f]{64}$",
        description="Digest returned by preflight for this exact selection; do not recompute it locally.",
    )

    @model_validator(mode="after")
    def nonblank_name(self):
        if not self.name.strip():
            raise ValueError("name must not be blank")
        self.name = self.name.strip()
        return self


class TrainingConfiguration(_RequestModel):
    schema_id: str = Field(
        min_length=1,
        max_length=150,
        description="Identity from the selected framework/base-model parameter schema.",
    )
    schema_digest: str = Field(
        pattern=r"^[0-9a-f]{64}$",
        description="Unchanged digest paired with schema_id from the server parameter schema.",
    )
    values: dict[str, JsonValue] = Field(default_factory=dict, max_length=150)


class TrainingParameters(_RequestModel):
    epochs: int = Field(default=50, ge=1, le=1000)
    batch: int = Field(default=8, ge=1, le=64)
    imgsz: int = Field(default=640, ge=128, le=1280, multiple_of=32)
    seed: int = Field(default=42, ge=0, le=2_147_483_647)
    lr0: float = Field(default=0.01, gt=0, le=0.1)
    patience: int = Field(default=50, ge=0, le=1000)


class TrainingOrigin(_RequestModel):
    kind: Literal["recipe", "clone"]
    recipe_id: uuid.UUID | None = None
    revision_id: uuid.UUID | None = None
    run_id: uuid.UUID | None = None
    attempt_id: uuid.UUID | None = None
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def exact_source(self):
        if self.kind == "recipe":
            if (
                not self.recipe_id
                or not self.revision_id
                or self.run_id
                or self.attempt_id
            ):
                raise ValueError(
                    "recipe origin requires recipe_id and revision_id only"
                )
        elif not self.run_id or self.recipe_id or self.revision_id:
            raise ValueError(
                "clone origin requires run_id and cannot include recipe IDs"
            )
        return self


class TrainingInitialization(_RequestModel):
    kind: Literal["catalog", "trained_model"]
    model_version_id: uuid.UUID | None = None
    source_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def controlled_source(self):
        if self.kind == "catalog":
            if self.model_version_id is not None or self.source_digest is not None:
                raise ValueError("catalog initialization cannot name a project model")
        elif self.model_version_id is None or self.source_digest is None:
            raise ValueError(
                "trained_model initialization requires its model version and digest"
            )
        return self


class TrainingRunCreate(_RequestModel):
    name: str = Field(min_length=1, max_length=120)
    task: Literal["detect", "segment"] | None = None
    dataset_id: uuid.UUID = Field(
        description="Frozen training-version UUID, not an uploaded storage dataset, task stream, or display ordinal."
    )
    framework: Literal["ultralytics", "libreyolo"] = "ultralytics"
    base_model: str = Field(default="yolo11n", pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    parameters: TrainingParameters = Field(default_factory=TrainingParameters)
    configuration: TrainingConfiguration | None = None
    initialization: TrainingInitialization | None = None
    origin: TrainingOrigin | None = None
    request_id: uuid.UUID = Field(
        description="Caller-generated idempotency UUID; keep the exact request for an uncertain replay."
    )
    preflight_digest: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
        description="Optional for preflight, required by create; copy the returned config_digest unchanged.",
    )

    @model_validator(mode="after")
    def coherent_source(self):
        if not self.name.strip():
            raise ValueError("name must not be blank")
        self.name = self.name.strip()
        if self.configuration is not None and "parameters" in self.model_fields_set:
            raise ValueError(
                "configuration and legacy parameters cannot be sent together"
            )
        if self.origin is not None and self.configuration is None:
            raise ValueError("a reusable origin requires a complete configuration")
        if (
            self.initialization is not None
            and self.initialization.kind == "trained_model"
            and self.configuration is None
        ):
            raise ValueError(
                "trained model initialization requires a complete configuration"
            )
        return self


class TrainingConfigurationResolve(_RequestModel):
    framework: Literal["ultralytics", "libreyolo"]
    base_model: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    configuration: TrainingConfiguration
    initialization: TrainingInitialization | None = None


class TrainingRecipeWrite(_RequestModel):
    request_id: uuid.UUID
    expected_head_revision: int = Field(default=0, ge=0)
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)
    configuration: TrainingConfigurationResolve | None = None
    source_run_id: uuid.UUID | None = None
    source_attempt_id: uuid.UUID | None = None
    origin: TrainingOrigin | None = None

    @model_validator(mode="after")
    def coherent_source(self):
        if not self.name.strip():
            raise ValueError("name must not be blank")
        self.name = self.name.strip()
        if (self.configuration is None) == (self.source_run_id is None):
            raise ValueError("choose exactly one configuration or source_run_id")
        if self.source_attempt_id is not None and self.source_run_id is None:
            raise ValueError("source_attempt_id requires source_run_id")
        if self.source_run_id is not None and self.origin is not None:
            raise ValueError("source_run_id cannot be combined with origin")
        return self


class TrainingPreviewCreate(_RequestModel):
    dataset_id: uuid.UUID
    framework: Literal["ultralytics", "libreyolo"]
    base_model: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    configuration: TrainingConfiguration
    seed: int = Field(default=42, ge=0, le=2_147_483_647, strict=True)
    epoch: int = Field(default=1, ge=1, le=1000, strict=True)
    count: int = Field(default=1, ge=1, le=4, strict=True)
    request_id: uuid.UUID


class TrainingTaskDatasetCapability(_AAPModel):
    supported: bool
    reason: str | None = None


class TrainingTaskDatasetCapabilities(_AAPModel):
    create_version: TrainingTaskDatasetCapability
    qualified_training: TrainingTaskDatasetCapability
    overview_statistics: TrainingTaskDatasetCapability
    task_statistics: TrainingTaskDatasetCapability
    sample_preview: TrainingTaskDatasetCapability
    semantic_comparison: TrainingTaskDatasetCapability


class TrainingDatasetSummary(_AAPModel):
    id: uuid.UUID
    project_id: uuid.UUID
    name: str
    task_dataset_id: uuid.UUID | None = None
    version_ordinal: int | None = None
    contract_profile: dict[str, Any] | None = None
    task: Literal["detect", "segment"] = "detect"
    media_type: str = "image"
    format: str = "ultralytics-yolo"
    source_media_type: str = "image"
    summary: dict[str, Any] = Field(default_factory=dict)
    class_count: int = 0
    annotation_scope: dict[str, Any] = Field(default_factory=dict)
    creator_id: uuid.UUID | None = None
    parent_id: uuid.UUID | None = None
    async_job_id: uuid.UUID | None = None
    status: str
    progress_pct: int
    snapshot_digest: str
    error_code: str | None = None
    error_message: str | None = None
    size_bytes: int | None = None
    package_sha256: str | None = None
    created_at: datetime
    completed_at: datetime | None = None


class TrainingDatasetOut(TrainingDatasetSummary):
    manifest: dict[str, Any]


class TrainingDatasetPage(_AAPModel):
    items: list[TrainingDatasetSummary]
    total: int


class TrainingDatasetPreflight(_AAPModel):
    ready: bool
    digest: str
    classes: list[str]
    summary: dict[str, int]
    issues: list[dict[str, Any]]
    excluded: list[dict[str, Any]]
    task: Literal["detect", "segment"] = "detect"
    media_type: Literal["image"] = "image"
    format: str = "ultralytics-yolo"
    annotation_scope: dict[str, Any] = Field(default_factory=dict)
    source: dict[str, Any] = Field(default_factory=dict)


class TrainingTaskDataset(_AAPModel):
    id: uuid.UUID | None
    project_id: uuid.UUID
    task: Literal["detect", "segment"]
    status_counts: dict[str, int]
    version_count: int
    pending_membership_count: int
    latest_ready_version: TrainingDatasetSummary | None
    capabilities: TrainingTaskDatasetCapabilities


class TrainingTaskDatasetPage(_AAPModel):
    items: list[TrainingTaskDataset]
    pending_membership_count: int
    unrecognized_version_count: int


class UnrecognizedTrainingDatasetVersion(_AAPModel):
    id: uuid.UUID
    name: str
    status: str
    created_at: datetime
    raw_task: str | None
    error_code: str = "unknown_data_contract"
    reason: str = ""


class UnrecognizedTrainingDatasetVersionPage(_AAPModel):
    items: list[UnrecognizedTrainingDatasetVersion]
    total: int = Field(ge=0)


class TrainingDatasetInsightOverview(_AAPModel):
    version_id: uuid.UUID
    schema_version: int
    input_digest: str
    snapshot_digest: str
    manifest_digest: str
    status: Literal["complete", "partial", "unavailable"]


class TrainingDatasetClassStatistics(_AAPModel):
    index: int
    name: str
    train_images: int
    val_images: int
    train_instances: int
    val_instances: int


class TrainingDatasetClassPage(_AAPModel):
    items: list[TrainingDatasetClassStatistics]
    total: int
    limit: int
    offset: int
    order_by: Literal["index", "instances", "images"] = "index"
    split: Literal["train", "val"] | None = None


class TrainingDatasetSample(_AAPModel):
    version_id: uuid.UUID
    task_id: uuid.UUID
    included: bool
    exclusion_reason: str | None
    issue: str | None
    sha256: str | None
    label_sha256: str | None
    split: Literal["train", "val"] | None
    width: int | None
    height: int | None
    size_bytes: int | None
    instances: int | None
    is_negative: bool | None
    explicit_negative: bool | None
    class_ids: list[int]
    media_state: Literal["unchecked", "verified", "unavailable"]
    media_checked_at: datetime | None


class TrainingDatasetSamplePage(_AAPModel):
    source_version_id: uuid.UUID
    items: list[TrainingDatasetSample]
    total: int
    limit: int
    offset: int


class TrainingDatasetSampleDetail(_AAPModel):
    sample: TrainingDatasetSample
    labels: list[dict[str, Any]]
    total: int | None
    limit: int
    offset: int


class TrainingDatasetInsightComparison(_AAPModel):
    baseline_id: uuid.UUID
    version_id: uuid.UUID
    status: Literal["complete", "partial", "unavailable"]
    counts: dict[str, int]
    count_unit: Literal["unique_contents"]
    baseline_members: int
    target_members: int


class TrainingCapabilities(_AAPModel):
    tasks: list[str]
    resume: bool
    artifact_download: bool
    model_registration: bool


class TrainingBaseModel(_AAPModel):
    id: str
    sha256: str
    filename: str
    task: Literal["detect", "segment"] = "detect"
    capabilities: TrainingCapabilities | None = None


class TrainingParameterField(_AAPModel):
    key: str
    label: str
    group: str
    status: str
    native_type: str
    default: JsonValue
    native_default: JsonValue
    defined_in: str
    reason: str
    rules: dict[str, JsonValue]
    aliases: list[str]


class TrainingSchemaSummary(_AAPModel):
    schema_id: str
    schema_digest: str
    framework: str
    framework_version: str
    adapter_revision: int
    task: str
    model_family: str
    field_count: int
    editable_count: int
    compatible: bool | None = None


class TrainingParameterSchema(_AAPModel):
    schema_id: str
    schema_digest: str
    framework: str
    framework_version: str
    adapter_revision: int
    task: str
    model_family: str
    source: str
    source_sha256: str
    groups: dict[str, str]
    fields: list[TrainingParameterField]


class TrainingTemplate(_AAPModel):
    framework: Literal["ultralytics", "libreyolo"]
    version: str
    image_digest: str
    base_models: list[TrainingBaseModel]
    capabilities: TrainingCapabilities
    compatible: bool | None = None
    parameter_schemas: list[TrainingSchemaSummary] = Field(default_factory=list)


class TrainingHubCatalog(_AAPModel):
    enabled: bool
    online: bool
    busy: bool
    resource_id: str | None
    executor_id: str | None = None
    last_seen_at: datetime | None = None
    items: list[TrainingTemplate]


class TrainingProjectSummary(_AAPModel):
    id: uuid.UUID
    name: str
    display_id: str | None
    data_type: str
    status: str
    can_prepare_dataset: bool
    preparation_reason: str | None
    ready_dataset_count: int
    active_run_count: int


class TrainingProjectPage(_AAPModel):
    total: int
    items: list[TrainingProjectSummary]


class TrainingQueueCounts(_AAPModel):
    active: int
    queued: int
    running: int
    succeeded: int
    failed: int
    cancelled: int


class TrainingQueueItem(_AAPModel):
    run: "TrainingRunOut"
    project_name: str
    project_display_id: str | None


class TrainingQueuePage(_AAPModel):
    total: int
    counts: TrainingQueueCounts
    items: list[TrainingQueueItem]


class TrainingRunOut(_AAPModel):
    id: uuid.UUID
    project_id: uuid.UUID
    creator_id: uuid.UUID | None
    dataset_id: uuid.UUID
    initialization_model_id: uuid.UUID | None = None
    async_job_id: uuid.UUID | None
    auto_trigger_id: uuid.UUID | None = None
    study_id: uuid.UUID | None = None
    name: str
    status: str
    task: Literal["detect", "segment"] = "detect"
    media_type: Literal["image"] = "image"
    config: dict[str, Any]
    config_digest: str
    origin: dict[str, Any] | None = None
    current_attempt_id: uuid.UUID | None
    metrics: dict[str, Any]
    error_message: str | None
    created_at: datetime
    completed_at: datetime | None


class TrainingFailure(_AAPModel):
    code: str
    stage: str
    config_digest: str
    diagnostic: str
    exception_type: str | None = None
    source: str


class TrainingAttemptOut(_AAPModel):
    id: uuid.UUID
    ordinal: int
    status: str
    resume_from_id: uuid.UUID | None
    executor_id: str | None
    resource_id: str | None
    runtime: dict[str, Any]
    artifacts: dict[str, Any]
    error_message: str | None
    heartbeat_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    failure: TrainingFailure | None = None
    native_budget: dict[str, Any] | None = None
    native_accounting: dict[str, Any] | None = None


class TrainingModelReturnEligibility(_AAPModel):
    qualified: bool
    reason: str | None
    qualification: dict[str, JsonValue] | None


class TrainingRunDetail(_AAPModel):
    run: TrainingRunOut
    attempts: list[TrainingAttemptOut]
    registration_eligibility: TrainingModelReturnEligibility | None = None


class TrainingRunPage(_AAPModel):
    total: int
    items: list[TrainingRunOut]


class TrainingRunPreflight(_AAPModel):
    config: dict[str, Any]
    config_digest: str
    origin: dict[str, Any] | None


class TrainingInitializationEligibility(_AAPModel):
    model_version_id: uuid.UUID
    eligible: bool
    reasons: list[str]
    pin: dict[str, Any] | None = None
    storage_verified: bool = False


class TrainingRunEvent(_AAPModel):
    sequence: int
    kind: str


class TrainingRunEvents(_AAPModel):
    attempt_id: uuid.UUID
    items: list[TrainingRunEvent]


class TrainingEvaluation(_AAPModel):
    task: Literal["detect", "segment"] = "detect"
    identity_status: Literal["verified", "unknown"]
    reasons: list[str]
    descriptor: dict[str, Any] | None
    compatibility_digest: str | None
    summary: dict[str, float | None]
    per_class_available: bool
    per_class_total: int
    per_class: list[dict[str, Any]]
    provenance: dict[str, JsonValue]


class TrainingCursorGap(_AAPModel):
    after: int
    through: int
    reason: Literal["retention", "producer_retention"]


class TrainingObservationPage(_AAPModel):
    attempt_id: uuid.UUID
    stream_id: uuid.UUID | None
    earliest_cursor: int | None
    latest_cursor: int
    next_cursor: int
    has_more: bool
    gaps: list[TrainingCursorGap]
    summary: dict[str, Any] | None


class TrainingTelemetryItem(_AAPModel):
    sequence: int
    producer_boot_id: uuid.UUID
    captured_at: datetime
    phase: str
    phase_elapsed_seconds: float
    callback_age_seconds: float | None
    metrics: dict[str, dict[str, Any]]
    progress: dict[str, Any]
    received_at: datetime


class TrainingTelemetryPage(TrainingObservationPage):
    items: list[TrainingTelemetryItem]
    metric_definitions: dict[str, dict[str, str]]


class TrainingLogItem(_AAPModel):
    sequence: int
    producer_boot_id: uuid.UUID
    captured_at: datetime
    text: str
    received_at: datetime


class TrainingLogPage(TrainingObservationPage):
    items: list[TrainingLogItem]


class TrainingRecovery(_AAPModel):
    attempt_id: uuid.UUID | None
    eligible: bool
    reasons: list[str]
    executor_online: bool
    runtime_compatible: bool
    checkpoint_epoch: int | None


class TrainingArtifactDownload(_AAPModel):
    url: str
    sha256: str | None = None
    size_bytes: int | None = None


class TrainingDatasetDownload(_AAPModel):
    url: str
    sha256: str


class TrainingRecipeRevisionOut(_AAPModel):
    id: uuid.UUID
    recipe_id: uuid.UUID
    project_id: uuid.UUID
    creator_id: uuid.UUID | None
    ordinal: int
    name: str
    description: str
    snapshot: dict[str, Any]
    snapshot_digest: str
    origin: dict[str, Any] | None
    initialization_model_id: uuid.UUID | None = None
    created_at: datetime


class TrainingRecipeOut(_AAPModel):
    id: uuid.UUID
    project_id: uuid.UUID
    head_revision: int
    archived: bool
    revision: TrainingRecipeRevisionOut
    compatible: bool
    incompatibility: str | None
    source_available: bool


class TrainingRecipePage(_AAPModel):
    total: int
    items: list[TrainingRecipeOut]


class TrainingReuseCandidate(_AAPModel):
    snapshot: dict[str, Any]
    snapshot_digest: str
    origin: TrainingOrigin
    label: str
    compatible: bool
    incompatibility: str | None
    source_dataset_id: uuid.UUID | None = None


class TrainingConfigurationSnapshot(_AAPModel):
    framework: Literal["ultralytics", "libreyolo"]
    framework_version: str
    image_digest: str
    base_model: dict[str, Any]
    configuration: dict[str, Any]
    initialization: dict[str, Any] | None = None


class TrainingPreviewCapabilities(_AAPModel):
    qualified: bool
    online: bool
    busy: bool
    resource_id: str
    reason: str | None
    policy_digest: str
    policy: dict[str, JsonValue]


class TrainingPreviewOut(_AAPModel):
    id: uuid.UUID
    project_id: uuid.UUID
    async_job_id: uuid.UUID | None
    request: TrainingPreviewCreate
    input_digest: str
    config_digest: str
    configuration: dict[str, JsonValue]
    status: str
    error_message: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    expires_at: datetime
    report: dict[str, Any] | None = None


TrainingRunOut.model_rebuild()
TrainingQueueItem.model_rebuild()
