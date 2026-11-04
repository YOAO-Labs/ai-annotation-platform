from __future__ import annotations

from typing import Literal
from uuid import UUID

from yoao_protocol_v2 import NativeMaskCandidate
from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from app.schemas.annotation import AnnotationOut
from app.schemas.prediction import PredictionOut


class AiMaskCandidateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate: NativeMaskCandidate
    candidate_index: StrictInt = Field(ge=0, le=1024)
    prompt_revision: str = Field(min_length=1, max_length=256)
    receipt: str = Field(min_length=16, max_length=4096)


class AiMaskPromptSummary(BaseModel):
    """Bounded, non-sensitive prompt lineage; coordinates never enter persistence."""

    model_config = ConfigDict(extra="forbid")

    family: Literal[
        "point",
        "interactive_box",
        "exemplar",
        "mask",
        "scribble",
        "correction_frame",
    ]
    positive_points: StrictInt = Field(default=0, ge=0, le=8192)
    negative_points: StrictInt = Field(default=0, ge=0, le=8192)
    boxes: StrictInt = Field(default=0, ge=0, le=64)
    positive_scribbles: StrictInt = Field(default=0, ge=0, le=64)
    negative_scribbles: StrictInt = Field(default=0, ge=0, le=64)
    multimask: bool = False
    parameters_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _validate_totals(self):
        if self.positive_points + self.negative_points > 8192:
            raise ValueError("total prompt points must be <= 8192")
        if self.positive_scribbles + self.negative_scribbles > 64:
            raise ValueError("total prompt scribbles must be <= 64")
        return self


class AiMaskRoutingLineage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requested_backend_id: UUID
    backend_pool_id: UUID | None = None
    backend_instance_id: UUID
    model_id: str = Field(min_length=1, max_length=128)


class AiMaskInferenceLineage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_version: str | None = Field(default=None, max_length=100)
    inference_time_ms: float | None = Field(default=None, ge=0, le=86_400_000)
    cache_hit: bool | None = None
    model_load_ms: float | None = Field(default=None, ge=0, le=86_400_000)


class AiMaskAcceptTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["create", "refine"] = "create"
    source_annotation_id: UUID | None = None
    source_version: StrictInt | None = Field(default=None, ge=1)
    frame_index: StrictInt | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _validate_mode_fields(self):
        if self.mode == "create":
            if self.source_annotation_id is not None or self.source_version is not None:
                raise ValueError(
                    "create target cannot include source annotation fields"
                )
        elif self.source_annotation_id is None or self.source_version is None:
            raise ValueError(
                "refine target requires source_annotation_id and source_version"
            )
        return self


class AiMaskAcceptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(
        min_length=16,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    candidate: AiMaskCandidateInput
    class_name: str = Field(min_length=1, max_length=100)
    video_segment_id: UUID | None = None
    target: AiMaskAcceptTarget = Field(default_factory=AiMaskAcceptTarget)
    prompt_summary: AiMaskPromptSummary
    routing: AiMaskRoutingLineage
    inference: AiMaskInferenceLineage = Field(default_factory=AiMaskInferenceLineage)


class AiMaskAcceptResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prediction: PredictionOut
    annotation: AnnotationOut
    source_version: int | None = None
    result_version: int
    content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    replayed: bool = False
