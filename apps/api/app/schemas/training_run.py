"""Manager and trusted executor contracts; parameters are deliberately bounded."""

import math
import re
import uuid
from datetime import datetime
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    RootModel,
    field_validator,
    model_validator,
)
from app.schemas.training_initialization import TrainingInitialization
from training_contract.dataset import config_task
from aap_task_contract import normalize_task, normalize_task_fields


class TrainingConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    schema_id: str = Field(min_length=1, max_length=150)
    schema_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    values: dict[str, JsonValue] = Field(default_factory=dict, max_length=150)


class TrainingParameters(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    epochs: int = Field(default=50, ge=1, le=1000)
    batch: int = Field(default=8, ge=1, le=64)
    imgsz: int = Field(default=640, ge=128, le=1280, multiple_of=32)
    seed: int = Field(default=42, ge=0, le=2147483647)
    lr0: float = Field(default=0.01, gt=0, le=0.1)
    patience: int = Field(default=50, ge=0, le=1000)


class TrainingOrigin(BaseModel):
    model_config = ConfigDict(extra="forbid")
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
                raise ValueError("配方来源需要配方与修订标识")
        elif not self.run_id or self.recipe_id or self.revision_id:
            raise ValueError("克隆来源需要实验标识")
        return self


class TrainingRunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    task: Literal["detect", "segment"] | None = None
    dataset_id: uuid.UUID
    framework: Literal["ultralytics", "libreyolo"] = "ultralytics"
    base_model: str = Field(default="yolo11n", pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    parameters: TrainingParameters = Field(default_factory=TrainingParameters)
    configuration: TrainingConfiguration | None = None
    initialization: TrainingInitialization | None = None
    origin: TrainingOrigin | None = None
    request_id: uuid.UUID
    preflight_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="before")
    @classmethod
    def canonical_task(cls, value):
        return normalize_task_fields(value)

    @field_validator("name")
    @classmethod
    def strip_name(cls, value):
        if not value.strip():
            raise ValueError("实验名称不能为空")
        return value.strip()

    @model_validator(mode="after")
    def one_parameter_source(self):
        if self.configuration is not None and "parameters" in self.model_fields_set:
            raise ValueError("configuration 与旧 parameters 不能同时提交")
        if self.origin is not None and self.configuration is None:
            raise ValueError("复用来源必须携带完整配置")
        if (
            self.initialization
            and self.initialization.kind == "trained_model"
            and self.configuration is None
        ):
            raise ValueError("项目权重初始化必须携带完整配置")
        return self

    def request_payload(self):
        payload = self.model_dump(mode="json")
        if self.task is None:
            payload.pop("task")
        if self.configuration is None:
            # Preserve v1 digests exactly, including preflight_digest=None.
            payload.pop("configuration")
        else:
            payload.pop("parameters")
        if self.origin is None:
            payload.pop("origin")
        if self.initialization is None or self.initialization.kind == "catalog":
            payload.pop("initialization")
        else:
            payload["initialization"] = self.initialization.model_dump(
                mode="json", exclude_none=True
            )
        return payload


class TrainingRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
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
    config: dict

    @model_validator(mode="after")
    def canonical_task_view(self):
        self.task = config_task(self.config)
        return self

    config_digest: str
    origin: dict | None = None
    current_attempt_id: uuid.UUID | None
    metrics: dict
    error_message: str | None
    created_at: datetime
    completed_at: datetime | None


class TrainingFailure(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: Literal[
        "input_mismatch",
        "config_mismatch",
        "runtime_mismatch",
        "cuda_oom",
        "native_exception",
        "native_signal",
        "preparation_failed",
        "upload_failed",
        "execution_lost",
        "authority_lost",
        "unknown",
        "budget_exhausted",
    ]
    stage: Literal["preparing", "training", "evaluating", "uploading", "execution"]
    config_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    diagnostic: str = Field(max_length=1000)
    exception_type: str | None = Field(default=None, max_length=120)
    source: Literal["native", "supervisor", "platform"]


class TrainingAttemptOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    ordinal: int
    status: str
    resume_from_id: uuid.UUID | None
    executor_id: str | None
    resource_id: str | None
    runtime: dict
    artifacts: dict
    error_message: str | None
    heartbeat_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    failure: TrainingFailure | None = None
    native_budget: dict | None = None
    native_accounting: dict | None = None


class EvaluationMetricSemantics(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    unit: Literal["fraction"]
    direction: Literal["higher"]
    minimum: Literal[0]
    maximum: Literal[1]
    unavailable_below: Literal[0]


class EvaluationCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    weights_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    selection: Literal["native_best"]
    best_epoch: int | None = Field(default=None, ge=1, le=1000)


class EvaluationBenchmark(BaseModel):
    model_config = ConfigDict(extra="forbid")
    validation_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    classes: list[str] = Field(min_length=1, max_length=10000)
    class_space: Literal["frozen_native"]


class EvaluationEvaluator(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    framework: Literal["ultralytics", "libreyolo"]
    version: str = Field(min_length=1, max_length=100)
    task: Literal["detection", "instance-segmentation"]
    implementation: str = Field(min_length=1, max_length=200)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    options: dict[str, JsonValue] = Field(min_length=1, max_length=100)


class ModelReturnEligibility(BaseModel):
    qualified: bool
    reason: str | None
    qualification: dict[str, JsonValue] | None


class TrainingRegisteredModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    attempt_id: uuid.UUID
    name: str


class TrainingRunDetail(BaseModel):
    run: TrainingRunOut
    attempts: list[TrainingAttemptOut]
    registration_eligibility: ModelReturnEligibility | None = None
    registered_models: list[TrainingRegisteredModel] = Field(default_factory=list)


class TrainingEvaluationDescriptor(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: Literal[1, 2, 3]
    config_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate: EvaluationCandidate
    benchmark: EvaluationBenchmark
    evaluator: EvaluationEvaluator
    semantics: dict[str, EvaluationMetricSemantics] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def task_revision(self):
        segmentation = self.evaluator.task == "instance-segmentation"
        if (
            self.revision not in ({2, 3} if segmentation else {1})
            or segmentation
            and self.evaluator.framework != "ultralytics"
        ):
            raise ValueError("评估任务、框架与 descriptor 修订不匹配")
        return self


class TrainingClassEvaluation(BaseModel):
    index: int
    name: str
    metrics: dict[str, float | None]


class TrainingEvaluationOut(BaseModel):
    task: Literal["detect", "segment"] = "detect"

    @model_validator(mode="after")
    def canonical_task_view(self):
        if self.descriptor is not None:
            self.task = normalize_task(self.descriptor.evaluator.task)
        return self

    identity_status: Literal["verified", "unknown"]
    reasons: list[str]
    descriptor: TrainingEvaluationDescriptor | None
    compatibility_digest: str | None
    summary: dict[str, float | None]
    per_class_available: bool
    per_class_total: int
    per_class: list[TrainingClassEvaluation]
    provenance: dict[str, JsonValue]


class ExecutorClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")
    executor_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,100}$")
    boot_id: uuid.UUID
    claim_id: uuid.UUID
    recover_only: bool = False
    resource_id: str = Field(min_length=1, max_length=200)
    runtime: dict


class ExecutorResumeArtifact(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str | None = None
    role: Literal["checkpoint"]
    sha256: str
    size_bytes: int
    key: str
    url: str
    epoch: int | None = None


class ExecutorInitializationArtifact(BaseModel):
    model_config = ConfigDict(extra="allow")

    key: str
    role: Literal["weights"]
    sha256: str
    size_bytes: int
    source_digest: str
    url: str


class ExecutorAssignment(BaseModel):
    model_config = ConfigDict(extra="allow")

    run_id: uuid.UUID
    attempt_id: uuid.UUID
    lease_token: str
    config: dict[str, JsonValue]
    config_digest: str
    dataset_url: str
    resume: ExecutorResumeArtifact | None
    native_budget: dict[str, JsonValue] | None = None
    initialization: ExecutorInitializationArtifact | None = None


class ExecutorClaimResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    assignment: ExecutorAssignment | None


class ExecutorHeartbeatResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    stop: bool
    reason: str | None


class ExecutorUploadGrant(BaseModel):
    model_config = ConfigDict(extra="allow")

    url: str
    headers: dict[str, str]


class ExecutorUploadResponse(RootModel[dict[str, ExecutorUploadGrant]]):
    pass


class ExecutorRecoveryResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    recovered: Literal[True]


class ExecutorFence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    executor_id: str = Field(min_length=1, max_length=100)
    boot_id: uuid.UUID
    lease_token: str = Field(min_length=32, max_length=128)


class ExecutorEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    sequence: int = Field(ge=1, le=100000)
    kind: Literal["preparing", "training", "epoch", "evaluating", "uploading"]
    epoch: int | None = Field(default=None, ge=1, le=1000)
    metrics: dict[str, float] = Field(default_factory=dict, max_length=100)

    @field_validator("metrics")
    @classmethod
    def finite_metrics(cls, value):
        if any(not math.isfinite(v) or len(k) > 100 for k, v in value.items()):
            raise ValueError("指标必须为有限数值")
        return value


class ExecutorHeartbeat(ExecutorFence):
    events: list[ExecutorEvent] = Field(default_factory=list, max_length=100)


class TrainingArtifactSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,99}$")
    role: Literal[
        "weights", "checkpoint", "metrics", "config", "log", "manifest", "telemetry"
    ]
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(gt=0, le=2 * 1024 * 1024 * 1024)
    epoch: int | None = Field(default=None, ge=1, le=1000)

    @model_validator(mode="after")
    def bounded_observation_artifact(self):
        if self.role == "telemetry" and self.size_bytes > 256 * 1024:
            raise ValueError("观测摘要产物超出上限")
        return self

    @field_validator("name")
    @classmethod
    def no_hidden_path(cls, value):
        if ".." in value or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,99}", value):
            raise ValueError("产物文件名不合法")
        return value


class ExecutorUpload(ExecutorFence):
    artifacts: list[TrainingArtifactSpec] = Field(min_length=1, max_length=12)


class ExecutorFinish(ExecutorFence):
    status: Literal["succeeded", "failed", "cancelled"]
    process_exited: Literal[True]
    error_message: str | None = Field(default=None, max_length=1000)
    failure: TrainingFailure | None = None
    native_accounting: dict | None = None

    def request_payload(self):
        payload = self.model_dump(mode="json")
        if self.failure is None:
            payload.pop("failure")
        if self.native_accounting is None:
            payload.pop("native_accounting")
        return payload


class ExecutorRecovery(ExecutorFence):
    new_boot_id: uuid.UUID
    exclusive_resource_lock: Literal[True]
    process_exited: Literal[True]
