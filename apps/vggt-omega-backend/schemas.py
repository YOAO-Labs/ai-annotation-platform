"""Small artifact-only reconstruction contract; no annotation predictions."""

from __future__ import annotations

from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from typing_extensions import Self

from config import CAMERA_COUNT, MAX_DIMENSION, PROFILE

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Identifier = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_.:/-]+$")]
Matrix3 = Annotated[list[float], Field(min_length=9, max_length=9)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


def http_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise ValueError("transfer URL must be http(s) without user information")
    if parsed.fragment or len(value) > 16384:
        raise ValueError("invalid transfer URL")
    return value


class CameraInput(StrictModel):
    role: Identifier
    image_url: str
    sha256: Sha256
    width: int = Field(ge=1, le=MAX_DIMENSION)
    height: int = Field(ge=1, le=MAX_DIMENSION)

    _validate_url = field_validator("image_url")(http_url)


class UploadTarget(StrictModel):
    url: str
    headers: dict[str, str] = Field(default_factory=dict, max_length=16)

    _validate_url = field_validator("url")(http_url)

    @field_validator("headers")
    @classmethod
    def validate_headers(cls, value: dict[str, str]) -> dict[str, str]:
        allowed = {"content-type", "x-amz-server-side-encryption", "x-amz-checksum-sha256"}
        if any(
            name.lower() not in allowed or "\n" in item or "\r" in item
            for name, item in value.items()
        ):
            raise ValueError("unsupported staging upload header")
        if sum(len(name) + len(item) for name, item in value.items()) > 4096:
            raise ValueError("staging upload headers too large")
        return value


class CameraUploadTargets(StrictModel):
    role: Identifier
    depth: UploadTarget
    confidence: UploadTarget


class ReconstructRequest(StrictModel):
    protocol_version: Literal["1"] = "1"
    request_id: Identifier
    idempotency_key: Identifier
    input_digest: Sha256
    profile: Literal[PROFILE]
    cameras: list[CameraInput] = Field(min_length=CAMERA_COUNT, max_length=CAMERA_COUNT)
    upload_targets: list[CameraUploadTargets] = Field(
        min_length=CAMERA_COUNT, max_length=CAMERA_COUNT
    )

    @model_validator(mode="after")
    def validate_roles(self) -> Self:
        roles = [camera.role for camera in self.cameras]
        if len(set(roles)) != CAMERA_COUNT:
            raise ValueError("camera roles must be unique")
        if [target.role for target in self.upload_targets] != roles:
            raise ValueError("upload targets must use the same ordered camera roles")
        urls = [target.url for row in self.upload_targets for target in (row.depth, row.confidence)]
        if len(set(urls)) != CAMERA_COUNT * 2:
            raise ValueError("every artifact requires its own staging upload URL")
        return self


class ArrayDescriptor(StrictModel):
    sha256: Sha256
    byte_size: int = Field(gt=0)
    dtype: Literal["<f4"] = "<f4"
    shape: tuple[int, int]
    format: Literal["npy"] = "npy"


class Preprocessing(StrictModel):
    source_to_output: Matrix3
    output_to_source: Matrix3
    output_width: int
    output_height: int
    crop: tuple[int, int, int, int]
    resize: tuple[int, int]
    padding: tuple[int, int, int, int]
    pixel_convention: Literal["zero-based-center"] = "zero-based-center"


class CameraOutput(StrictModel):
    role: Identifier
    source_sha256: Sha256
    source_width: int
    source_height: int
    depth: ArrayDescriptor
    confidence: ArrayDescriptor
    preprocessing: Preprocessing


class Producer(StrictModel):
    source_commit: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    weights_sha256: Sha256
    preprocessing: Literal["balanced-480"] = "balanced-480"
    execution_shape: Literal["B6S1"] = "B6S1"


class Timings(StrictModel):
    model_load_ms: int | None
    inference_ms: int = Field(ge=0)
    total_ms: int = Field(ge=0)


class ReconstructResponse(StrictModel):
    protocol_version: Literal["1"] = "1"
    request_id: Identifier
    idempotency_key: Identifier
    input_digest: Sha256
    profile: Literal[PROFILE]
    producer: Producer
    cameras: list[CameraOutput]
    timings: Timings
    cache_hit: bool
