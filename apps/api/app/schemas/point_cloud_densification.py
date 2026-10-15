"""Bounded artifact-producer contracts for current-frame point-cloud assistance."""

from __future__ import annotations

import math
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


OMEGA_SOURCE_COMMIT = "48b23c8ce72c9a8fcf987f0d7f43d8e4870759f6"
OMEGA_WEIGHTS_SHA256 = (
    "c02da418b18bb01d0392598d3f6147366bcde1bb70fd08a5e3bf7925b0667934"
)
INFERENCE_PROFILE = "omega-current-mono-512"
GENERATION_PROFILE = "omega-current-mono-512-full-lidar"
DEFAULT_RECONSTRUCTION_TIMEOUT_SECONDS = 1800
SHA256 = r"^[0-9a-f]{64}$"


class ProducerProfile(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)

    id: Literal["omega-current-mono-512"]
    camera_count: Literal[6]
    execution_shape: Literal["B6S1"]
    max_width: int = Field(gt=0, le=4096)
    max_height: int = Field(gt=0, le=4096)
    outputs: list[Literal["depth", "confidence"]]
    request_timeout_seconds: float = Field(
        default=DEFAULT_RECONSTRUCTION_TIMEOUT_SECONDS, gt=0, allow_inf_nan=False
    )

    @model_validator(mode="after")
    def complete_outputs(self):
        if sorted(self.outputs) != ["confidence", "depth"]:
            raise ValueError("depth and confidence outputs are required exactly once")
        return self


class PointCloudDensificationCapability(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    protocol_version: Literal["1"]
    endpoint: Literal["/reconstruct"]
    profiles: list[ProducerProfile] = Field(min_length=1, max_length=1)


def reconstruction_timeout_seconds(capability: dict | None) -> float:
    if capability is None:
        return DEFAULT_RECONSTRUCTION_TIMEOUT_SECONDS
    return (
        PointCloudDensificationCapability.model_validate(capability)
        .profiles[0]
        .request_timeout_seconds
    )


class ArrayArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    sha256: str = Field(pattern=SHA256)
    byte_size: int = Field(gt=0, le=2 * 1024 * 1024)
    dtype: Literal["<f4"]
    shape: list[int] = Field(min_length=2, max_length=2)
    format: Literal["npy"]

    @model_validator(mode="after")
    def fixed_grid(self):
        if self.shape != [352, 640]:
            raise ValueError("unsupported producer grid")
        return self


class ImagePreprocessing(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    pixel_convention: Literal["zero-based-center"] = "zero-based-center"
    source_to_output: list[float] = Field(min_length=9, max_length=9)
    output_to_source: list[float] = Field(min_length=9, max_length=9)
    output_width: Literal[640]
    output_height: Literal[352]
    crop: list[int] = Field(min_length=4, max_length=4)
    resize: list[int] = Field(min_length=2, max_length=2)
    padding: list[int] = Field(min_length=4, max_length=4)

    @model_validator(mode="after")
    def finite_transforms(self):
        if not all(
            math.isfinite(v) for v in self.source_to_output + self.output_to_source
        ):
            raise ValueError("non-finite image transform")
        return self


class ReconstructedCamera(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    role: str = Field(min_length=1, max_length=50)
    source_sha256: str = Field(pattern=SHA256)
    source_width: int = Field(gt=0, le=4096)
    source_height: int = Field(gt=0, le=4096)
    depth: ArrayArtifact
    confidence: ArrayArtifact
    preprocessing: ImagePreprocessing


class ProducerIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    source_commit: Literal[OMEGA_SOURCE_COMMIT]
    weights_sha256: Literal[OMEGA_WEIGHTS_SHA256]
    preprocessing: Literal["balanced-480"]
    execution_shape: Literal["B6S1"]


class ProducerTimings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    model_load_ms: float | None = Field(ge=0, allow_inf_nan=False)
    inference_ms: float = Field(ge=0, allow_inf_nan=False)
    total_ms: float = Field(ge=0, allow_inf_nan=False)


class ReconstructionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    protocol_version: Literal["1"]
    request_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=128)
    input_digest: str = Field(pattern=SHA256)
    profile: Literal["omega-current-mono-512"]
    producer: ProducerIdentity
    cameras: list[ReconstructedCamera] = Field(min_length=6, max_length=6)
    timings: ProducerTimings
    cache_hit: bool

    @model_validator(mode="after")
    def unique_cameras(self):
        if len({camera.role for camera in self.cameras}) != 6:
            raise ValueError("duplicate camera role")
        return self


class PointCloudDensificationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: Literal["task_ids", "scene_id"]
    task_ids: list[UUID] = Field(default_factory=list, max_length=500)
    scene_id: UUID | None = None
    retry_job_id: UUID | None = None

    @model_validator(mode="after")
    def one_scope(self):
        if self.scope == "task_ids":
            if not self.task_ids or self.scene_id is not None:
                raise ValueError("task_ids scope requires tasks and no scene")
            if len(set(self.task_ids)) != len(self.task_ids):
                raise ValueError("duplicate task id")
        elif self.scene_id is None or self.task_ids:
            raise ValueError("scene_id scope requires one scene and no tasks")
        return self


class PointCloudPreparationResponse(BaseModel):
    job_id: UUID
    status: Literal["pending", "running", "completed", "failed", "cancelled"]
    total_tasks: int
    reused: bool = False


class PointCloudProducerOption(BaseModel):
    id: UUID
    name: str
    profile: str


class PointCloudSceneOption(BaseModel):
    id: UUID
    name: str


class PointCloudPreparationOptions(BaseModel):
    deployment_enabled: bool
    producers: list[PointCloudProducerOption]
    scenes: list[PointCloudSceneOption]
