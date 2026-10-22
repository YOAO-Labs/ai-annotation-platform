"""Frozen evaluation identities and bounded, independently verifiable evidence."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from aap_protocol_v2.mask_interaction import CocoRlePayload

from app.services.sample_evaluation_engine import (
    MAX_PREDICTIONS,
    MAX_SAMPLES,
    MAX_TRUTH_PER_IMAGE,
    MAX_TRUTH_TOTAL,
    checked_box,
)

Hash = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
UnitFloat = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False, strict=True)]
Index = Annotated[int, Field(ge=0, strict=True)]
EvaluationStatus = Literal[
    "queued", "running", "cancelling", "lost", "succeeded", "failed", "cancelled"
]
EvaluationRole = Literal["validation_selection", "held_out_test"]
ErrorFilter = Literal[
    "false_positive", "false_negative", "confusion", "localization", "failed"
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class SampleEvaluationCreate(StrictModel):
    request_id: uuid.UUID
    name: str = Field(min_length=1, max_length=120)
    model_version_id: uuid.UUID
    deployment_id: uuid.UUID
    dataset_id: uuid.UUID
    sample_task_ids: list[uuid.UUID] = Field(min_length=1, max_length=MAX_SAMPLES)
    role: EvaluationRole = "validation_selection"
    held_out_confirmed: bool = Field(default=False, strict=True)
    confidence: UnitFloat = 0.25
    matching_iou: UnitFloat = 0.5
    nms_iou: UnitFloat = 0.7

    @model_validator(mode="after")
    def validate_selection(self):
        if len(set(self.sample_task_ids)) != len(self.sample_task_ids):
            raise ValueError("样本任务不能重复；相同内容的不同冻结成员仍分别计数")
        if self.role == "held_out_test" and not self.held_out_confirmed:
            raise ValueError("独立测试集须明确确认未用于候选训练或模型选择")
        if not self.name.strip():
            raise ValueError("评估名称不能为空")
        self.name = self.name.strip()
        return self


class DetectionTruth(StrictModel):
    index: Index
    class_index: Index
    xyxy: list[UnitFloat] = Field(min_length=4, max_length=4)

    @model_validator(mode="after")
    def validate_geometry(self):
        if checked_box(self.xyxy) != self.xyxy:
            raise ValueError(
                "Detection evidence must use normalized logical coordinates"
            )
        return self


class DetectionPrediction(DetectionTruth):
    native_class_index: Index
    score: UnitFloat


class MaskGeometry(StrictModel):
    index: Index
    class_index: Index
    rle: CocoRlePayload
    mask_sha256: Hash
    area_pixels: Annotated[int, Field(gt=0, strict=True)]


class MaskTruth(MaskGeometry):
    annotation_id: uuid.UUID
    annotation_version: Annotated[int, Field(gt=0, strict=True)]
    rasterizer: str


class MaskPrediction(MaskGeometry):
    native_class_index: Index
    score: UnitFloat


class DetectionMatch(StrictModel):
    prediction_index: Index
    truth_index: Index
    iou: UnitFloat


class DetectionDiagnostic(DetectionMatch):
    kind: Literal["confusion", "localization"]


class MaskMatch(DetectionMatch):
    dice: UnitFloat


class MaskDiagnostic(MaskMatch):
    kind: Literal["confusion", "localization"]


class DetectionMetrics(StrictModel):
    tp: Index
    fp: Index
    fn: Index
    precision: UnitFloat | None
    recall: UnitFloat | None
    f1: UnitFloat | None


class DetectionMatching(DetectionMetrics):
    matches: list[DetectionMatch] = Field(max_length=MAX_PREDICTIONS)
    false_positive_indices: list[Index] = Field(max_length=MAX_PREDICTIONS)
    false_negative_indices: list[Index] = Field(max_length=MAX_TRUTH_PER_IMAGE)
    excluded_prediction_indices: list[Index] = Field(max_length=MAX_PREDICTIONS)
    diagnostics: list[DetectionDiagnostic] = Field(max_length=MAX_PREDICTIONS)


class MaskMatching(DetectionMatching):
    matches: list[MaskMatch] = Field(max_length=MAX_PREDICTIONS)
    diagnostics: list[MaskDiagnostic] = Field(max_length=MAX_PREDICTIONS)


class EvaluationProfile(StrictModel):
    schema_version: Literal[1] = Field(alias="schema")
    implementation: Literal[
        "aap-ultralytics-detection", "aap-ultralytics-instance-mask"
    ]
    revision: Literal[1, 2]
    ultralytics_version: str = Field(min_length=1, max_length=50)
    torch_version: str = Field(min_length=1, max_length=80)
    pillow_version: str = Field(min_length=1, max_length=50)
    source_digest: Hash
    storage_identity: uuid.UUID
    effective_device: str = Field(min_length=1, max_length=50)
    options: dict[str, Any]
    profile_digest: Hash


class FrozenEvaluationSample(StrictModel):
    ordinal: Annotated[int, Field(ge=0, lt=MAX_SAMPLES, strict=True)]
    task_id: uuid.UUID
    sha256: Hash
    label_sha256: Hash
    width: Annotated[int, Field(gt=0, strict=True)]
    height: Annotated[int, Field(gt=0, strict=True)]
    size_bytes: Annotated[int, Field(gt=0, strict=True)]
    split: Literal["val"]
    explicit_negative: bool | None
    truth: list[DetectionTruth | MaskTruth] = Field(max_length=MAX_TRUTH_PER_IMAGE)


class EvaluationSpec(StrictModel):
    schema_version: Literal[1, 2] = Field(alias="schema")
    evaluation_id: uuid.UUID
    model_ref: dict[str, Any]
    dataset_id: uuid.UUID
    snapshot_digest: Hash
    manifest_digest: Hash
    package_sha256: Hash
    benchmark_digest: Hash
    comparison_digest: Hash
    classes: list[str] = Field(min_length=1, max_length=1000)
    native_to_benchmark: list[Index] = Field(min_length=1, max_length=1000)
    role: EvaluationRole
    held_out_confirmed: bool
    confidence: UnitFloat
    matching_iou: UnitFloat
    nms_iou: UnitFloat
    evaluator: dict[str, Any]
    preprocessing: dict[str, Any]
    profile: EvaluationProfile
    profile_digest: Hash
    samples: list[FrozenEvaluationSample] = Field(min_length=1, max_length=MAX_SAMPLES)
    issued_at: Annotated[int, Field(gt=0, strict=True)]
    expires_at: Annotated[int, Field(gt=0, strict=True)]

    @model_validator(mode="after")
    def validate_budgets(self):
        expected_truth = MaskTruth if self.schema_version == 2 else DetectionTruth
        expected_profile = (
            "aap-ultralytics-instance-mask"
            if self.schema_version == 2
            else "aap-ultralytics-detection"
        )
        if self.profile.implementation != expected_profile or any(
            not isinstance(item, expected_truth)
            for sample in self.samples
            for item in sample.truth
        ):
            raise ValueError(
                "Evaluation task schema, profile and truth geometry differ"
            )
        if (
            len(set(self.classes)) != len(self.classes)
            or any(not name or len(name) > 120 for name in self.classes)
            or sorted(self.native_to_benchmark) != list(range(len(self.classes)))
            or [sample.ordinal for sample in self.samples]
            != list(range(len(self.samples)))
            or len({sample.task_id for sample in self.samples}) != len(self.samples)
            or sum(len(sample.truth) for sample in self.samples) > MAX_TRUTH_TOTAL
            or self.expires_at - self.issued_at != 86400
        ):
            raise ValueError(
                "Frozen evaluation specification violates identity or budget constraints"
            )
        for sample in self.samples:
            if [box.index for box in sample.truth] != list(
                range(len(sample.truth))
            ) or any(box.class_index >= len(self.classes) for box in sample.truth):
                raise ValueError("Frozen truth identity is invalid")
        return self


class EvaluationSampleResult(StrictModel):
    ordinal: Annotated[int, Field(ge=0, lt=MAX_SAMPLES, strict=True)]
    task_id: uuid.UUID
    sha256: Hash
    label_sha256: Hash
    width: Annotated[int, Field(gt=0, strict=True)]
    height: Annotated[int, Field(gt=0, strict=True)]
    status: Literal["pending", "succeeded", "failed"]
    truth: list[DetectionTruth | MaskTruth] = Field(max_length=MAX_TRUTH_PER_IMAGE)
    predictions: list[DetectionPrediction | MaskPrediction] = Field(
        default_factory=list, max_length=MAX_PREDICTIONS
    )
    matching: DetectionMatching | MaskMatching | None = None
    derived_sha256: Hash | None = None
    preprocessing: dict[str, Any] | None = None
    request_id: uuid.UUID
    binding_digest: Hash | None = None
    inference: dict[str, Any] | None = None
    produced_attempt_id: uuid.UUID | None = None
    error_code: str | None = Field(default=None, max_length=80)
    error_message: str | None = Field(default=None, max_length=1000)
    evidence_digest: Hash | None = None

    @model_validator(mode="after")
    def validate_completion(self):
        if self.status == "succeeded":
            if (
                not all(
                    (
                        self.derived_sha256,
                        self.preprocessing,
                        self.binding_digest,
                        self.inference,
                        self.produced_attempt_id,
                        self.evidence_digest,
                    )
                )
                or self.matching is None
                or self.error_code
                or self.error_message
            ):
                raise ValueError("Successful sample is missing verified evidence")
        elif self.predictions or self.matching is not None or self.evidence_digest:
            raise ValueError(
                "Incomplete sample cannot expose successful detection evidence"
            )
        if self.status == "failed" and not self.error_code:
            raise ValueError("Failed sample requires an explicit reason")
        return self


class PerClassMetrics(DetectionMetrics):
    class_index: Index
    name: str = Field(min_length=1, max_length=120)


class ConfusionCount(StrictModel):
    truth_class_index: Index
    prediction_class_index: Index
    count: Annotated[int, Field(gt=0, strict=True)]


class EvaluationAggregate(DetectionMetrics):
    samples: Annotated[int, Field(ge=1, le=MAX_SAMPLES, strict=True)]
    per_class: list[PerClassMetrics] = Field(min_length=1, max_length=1000)
    diagnostics: dict[Literal["confusion", "localization"], Index]
    confusion: list[ConfusionCount] = Field(max_length=MAX_PREDICTIONS * MAX_SAMPLES)


class MaskAreaMetrics(DetectionMetrics):
    group: Literal["small", "medium", "large"]


class MaskEvaluationAggregate(EvaluationAggregate):
    matched_mean_iou: UnitFloat | None
    matched_mean_dice: UnitFloat | None
    area_groups: list[MaskAreaMetrics] = Field(min_length=3, max_length=3)


class EvaluationReport(StrictModel):
    schema_version: Literal[1, 2] = Field(alias="schema")
    evaluation_id: uuid.UUID
    attempt_id: uuid.UUID
    spec_digest: Hash
    benchmark_digest: Hash
    comparison_digest: Hash
    model_ref: dict[str, Any]
    evaluator: dict[str, Any]
    status: Literal["succeeded", "failed", "cancelled"]
    complete: bool
    total_samples: Annotated[int, Field(ge=1, le=MAX_SAMPLES, strict=True)]
    succeeded_samples: Index
    failed_samples: Index
    pending_samples: Index
    aggregate: EvaluationAggregate | MaskEvaluationAggregate | None
    samples: list[EvaluationSampleResult] = Field(min_length=1, max_length=MAX_SAMPLES)

    @model_validator(mode="after")
    def validate_completion(self):
        counts = {
            state: sum(sample.status == state for sample in self.samples)
            for state in ("pending", "succeeded", "failed")
        }
        if (
            len(self.samples) != self.total_samples
            or [sample.ordinal for sample in self.samples]
            != list(range(self.total_samples))
            or counts
            != {
                "pending": self.pending_samples,
                "succeeded": self.succeeded_samples,
                "failed": self.failed_samples,
            }
            or self.complete != (self.status == "succeeded")
            or (
                self.complete
                and (
                    self.succeeded_samples != self.total_samples
                    or self.aggregate is None
                )
            )
            or (not self.complete and self.aggregate is not None)
        ):
            raise ValueError(
                "Partial evaluation cannot masquerade as a complete report"
            )
        return self


class EvaluationAttemptOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    ordinal: int
    generation: int
    status: EvaluationStatus
    heartbeat_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    error_code: str | None
    error_message: str | None
    report: dict[str, Any] | None


class SampleEvaluationOut(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    name: str
    model_version_id: uuid.UUID
    deployment_id: uuid.UUID
    dataset_id: uuid.UUID
    request_id: uuid.UUID
    status: EvaluationStatus
    spec_digest: Hash
    benchmark_digest: Hash
    comparison_digest: Hash
    spec: EvaluationSpec
    current_attempt_id: uuid.UUID | None
    async_job_id: uuid.UUID | None
    progress: dict[str, int]
    error_code: str | None
    error_message: str | None
    attempts: list[EvaluationAttemptOut]
    created_at: datetime
    completed_at: datetime | None
    deletion_requested_at: datetime | None


class EvaluationSamplePage(StrictModel):
    evaluation_id: uuid.UUID
    attempt_id: uuid.UUID
    report_sha256: Hash
    spec_digest: Hash
    total: Index
    offset: Index
    limit: Annotated[int, Field(ge=1, le=100, strict=True)]
    items: list[EvaluationSampleResult] = Field(max_length=32)


class EvaluationSourceOut(StrictModel):
    evaluation_id: uuid.UUID
    attempt_id: uuid.UUID
    ordinal: Index
    checked_at: datetime
    source: Literal["found", "deleted", "unavailable"]
    task_id: uuid.UUID | None
    project_id: uuid.UUID | None
    batch_id: uuid.UUID | None
    media: Literal["same", "changed", "unavailable"]
    labels: Literal["same", "changed", "unsupported", "unavailable"]
    current_sha256: Hash | None
    current_label_sha256: Hash | None
    current_width: int | None
    current_height: int | None
    task_status: str | None
    reviewed: bool | None
    negative_confirmed: bool | None
    reason: str | None
