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
    field_validator,
    model_validator,
)


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
    dataset_id: uuid.UUID
    framework: Literal["ultralytics", "libreyolo"] = "ultralytics"
    base_model: str = Field(default="yolo11n", pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    parameters: TrainingParameters = Field(default_factory=TrainingParameters)
    configuration: TrainingConfiguration | None = None
    origin: TrainingOrigin | None = None
    request_id: uuid.UUID
    preflight_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

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
        return self

    def request_payload(self):
        payload = self.model_dump(mode="json")
        if self.configuration is None:
            # Preserve v1 digests exactly, including preflight_digest=None.
            payload.pop("configuration")
        else:
            payload.pop("parameters")
        if self.origin is None:
            payload.pop("origin")
        return payload


class TrainingRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    project_id: uuid.UUID
    creator_id: uuid.UUID | None
    dataset_id: uuid.UUID
    async_job_id: uuid.UUID | None
    auto_trigger_id: uuid.UUID | None = None
    name: str
    status: str
    config: dict
    config_digest: str
    origin: dict | None = None
    current_attempt_id: uuid.UUID | None
    metrics: dict
    error_message: str | None
    created_at: datetime
    completed_at: datetime | None


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
    task: Literal["detection"]
    implementation: str = Field(min_length=1, max_length=200)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    options: dict[str, JsonValue] = Field(min_length=1, max_length=100)


class TrainingEvaluationDescriptor(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: Literal[1]
    config_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate: EvaluationCandidate
    benchmark: EvaluationBenchmark
    evaluator: EvaluationEvaluator
    semantics: dict[str, EvaluationMetricSemantics] = Field(min_length=1, max_length=20)


class TrainingClassEvaluation(BaseModel):
    index: int
    name: str
    metrics: dict[str, float | None]


class TrainingEvaluationOut(BaseModel):
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
    role: Literal["weights", "checkpoint", "metrics", "config", "log", "manifest"]
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(gt=0, le=2 * 1024 * 1024 * 1024)
    epoch: int | None = Field(default=None, ge=1, le=1000)

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


class ExecutorRecovery(ExecutorFence):
    new_boot_id: uuid.UUID
    exclusive_resource_lock: Literal[True]
    process_exited: Literal[True]
