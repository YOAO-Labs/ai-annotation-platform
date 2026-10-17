"""Typed, bounded public preview evidence and internal CPU protocol."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, RootModel, model_validator

from app.schemas.training_run import ExecutorFence, TrainingConfiguration


class TrainingPreviewCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dataset_id: uuid.UUID
    framework: Literal["ultralytics", "libreyolo"]
    base_model: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    configuration: TrainingConfiguration
    seed: int = Field(default=42, ge=0, le=2147483647, strict=True)
    epoch: int = Field(default=1, ge=1, le=1000, strict=True)
    count: int = Field(default=1, ge=1, le=4, strict=True)
    request_id: uuid.UUID


class PreviewBox(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    class_index: int = Field(ge=0, le=9999)
    xywh: list[float] = Field(min_length=4, max_length=4)

    @model_validator(mode="after")
    def normalized_geometry(self):
        x, y, w, h = self.xywh
        if (
            min(w, h) <= 0
            or min(x - w / 2, y - h / 2) < -1e-6
            or max(x + w / 2, y + h / 2) > 1 + 1e-6
        ):
            raise ValueError("Invalid native normalized box")
        return self


class PreviewOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    step: str = Field(min_length=1, max_length=50)
    clipped: int = Field(ge=0, le=100000)
    filtered: int = Field(ge=0, le=100000)


class PreviewOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    index: int = Field(ge=0, le=3)
    image: str = Field(pattern=r"^preview-[0-3]\.png$")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    width: int = Field(ge=1, le=2560)
    height: int = Field(ge=1, le=2560)
    boxes: list[PreviewBox] = Field(max_length=4096)
    source_draws: list[uuid.UUID] = Field(min_length=1, max_length=64)
    sources_read: list[uuid.UUID] = Field(min_length=1, max_length=64)
    operations: list[PreviewOperation] = Field(max_length=128)
    filtered: int = Field(ge=0, le=100000)
    clipped: int = Field(ge=0, le=100000)


class PreviewSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: uuid.UUID
    media_key: str = Field(min_length=1, max_length=500)
    image_path: str = Field(min_length=1, max_length=200)
    label_path: str = Field(min_length=1, max_length=200)
    labels: str = Field(max_length=64000)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    label_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(gt=0, le=8 * 1024**2)
    width: int = Field(gt=0, le=16000000)
    height: int = Field(gt=0, le=16000000)


class TrainingPreviewReport(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    revision: Literal[1]
    preview_id: uuid.UUID
    input_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    config_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy: dict[str, JsonValue]
    policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    seed: int
    epoch: int
    mosaic_closed: bool
    native_parameters: dict[str, JsonValue] = Field(max_length=160)
    transform_parameters: dict[str, float]
    source_pool: list[PreviewSource] = Field(min_length=1, max_length=16)
    pool_summary: dict[str, JsonValue]
    outputs: list[PreviewOutput] = Field(min_length=1, max_length=4)
    runtime: dict[str, JsonValue] = Field(max_length=30)


class TrainingPreviewOut(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    async_job_id: uuid.UUID | None
    request: TrainingPreviewCreate
    input_digest: str
    config_digest: str
    configuration: dict[str, JsonValue]
    status: Literal[
        "queued",
        "running",
        "cancelling",
        "lost",
        "succeeded",
        "failed",
        "cancelled",
        "expired",
    ]
    error_message: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    expires_at: datetime
    report: TrainingPreviewReport | None = None


class PreviewCapabilities(BaseModel):
    qualified: bool
    online: bool
    busy: bool
    resource_id: str
    reason: str | None
    policy_digest: str
    policy: dict[str, JsonValue]


class PreviewClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")
    purpose: Literal["augmentation-preview"]
    executor_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,100}$")
    boot_id: uuid.UUID
    claim_id: uuid.UUID
    resource_id: str = Field(min_length=1, max_length=200)
    runtime: dict[str, JsonValue] = Field(max_length=30)
    recover_only: bool = False


class PreviewExecutionSpec(BaseModel):
    model_config = ConfigDict(extra="allow")

    config: dict[str, JsonValue]
    config_digest: str
    native_values: dict[str, JsonValue]
    dataset_snapshot_digest: str
    policy_digest: str
    seed: int
    epoch: int
    count: int
    sources: list[PreviewSource]
    pool_summary: dict[str, JsonValue]


class PreviewAssignment(BaseModel):
    model_config = ConfigDict(extra="allow")

    preview_id: uuid.UUID
    lease_token: str
    input_digest: str
    spec: PreviewExecutionSpec
    source_urls: dict[str, str]
    stop: bool


class PreviewClaimResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    assignment: PreviewAssignment | None


class PreviewHeartbeatResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    stop: bool
    reason: str | None


class PreviewUploadGrant(BaseModel):
    model_config = ConfigDict(extra="allow")

    url: str
    headers: dict[str, str]


class PreviewUploadsResponse(RootModel[dict[str, PreviewUploadGrant]]):
    pass


class PreviewFinishResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: uuid.UUID
    status: Literal["succeeded", "failed", "cancelled"]


class PreviewArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(pattern=r"^(preview-[0-3]\.png|report\.json)$")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(gt=0, le=24 * 1024**2)


class PreviewUploads(ExecutorFence):
    artifacts: list[PreviewArtifact] = Field(min_length=2, max_length=5)


class PreviewFinish(ExecutorFence):
    status: Literal["succeeded", "failed", "cancelled"]
    process_exited: Literal[True]
    error_message: str | None = Field(default=None, max_length=1000)
