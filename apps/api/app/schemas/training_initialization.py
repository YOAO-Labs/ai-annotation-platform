"""Controlled selection and safe qualification projections for fresh training."""

import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from training_contract.initialization import validate_pin


class TrainingInitialization(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["catalog", "trained_model"]
    model_version_id: uuid.UUID | None = None
    source_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def controlled_source(self):
        if self.kind == "catalog":
            if self.model_version_id is not None or self.source_digest is not None:
                raise ValueError("目录初始化不能指定项目权重")
        elif self.model_version_id is None or self.source_digest is None:
            raise ValueError("项目权重初始化须指定模型版本与来源摘要")
        return self


class InitializationArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(min_length=1, max_length=500)
    role: Literal["weights"]
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(gt=0, le=2 * 1024**3, strict=True)


class InitializationRuntime(BaseModel):
    model_config = ConfigDict(extra="forbid")
    python: str
    torch: str
    torchvision: str
    cuda: str
    image_digest: str


class InitializationEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    report_sha256: str
    report_digest: str
    validation_digest: str


class FrozenTrainingInitialization(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["trained_model"]
    policy_digest: str
    project_id: uuid.UUID
    model_version_id: uuid.UUID
    model_name: str = Field(min_length=1, max_length=120)
    run_id: uuid.UUID
    attempt_id: uuid.UUID
    dataset_id: uuid.UUID
    framework: Literal["ultralytics"]
    framework_version: str
    task: Literal["image-detection", "detect"]
    model_family: Literal["yolov8", "yolo11", "yolo26"]
    base_model: str
    artifact: InitializationArtifact
    classes: list[str] = Field(min_length=1, max_length=10000)
    classes_digest: str
    bindings: list[str] = Field(min_length=1, max_length=10000)
    binding_digest: str
    config_digest: str
    dataset_digest: str
    validation_digest: str
    runtime: InitializationRuntime
    evaluation: InitializationEvaluation
    source_digest: str

    @model_validator(mode="after")
    def coherent_identity(self):
        validate_pin(self.model_dump(mode="json"))
        return self


class SegmentationInitializationEvaluation(InitializationEvaluation):
    normalized_model_digest: str
    descriptor_digest: str
    weights_sha256: str


class FrozenSegmentationTrainingInitialization(FrozenTrainingInitialization):
    task: Literal["segment"]
    data_contract: dict
    evaluation: SegmentationInitializationEvaluation


class TrainingInitializationEligibility(BaseModel):
    model_version_id: uuid.UUID
    eligible: bool
    reasons: list[str] = Field(max_length=20)
    pin: (
        FrozenTrainingInitialization | FrozenSegmentationTrainingInitialization | None
    ) = None
    storage_verified: bool = False
