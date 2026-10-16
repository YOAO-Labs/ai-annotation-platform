"""Bounded reads over frozen training data and derived metadata."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict


class TrainingDatasetDistributionBin(BaseModel):
    key: str
    label: str
    train: int
    val: int


class TrainingDatasetValidationIdentity(BaseModel):
    manifest_digest: str | None
    semantic_digest: str | None
    evaluation_multiset_digest: str | None
    ordered_classes_digest: str


class TrainingDatasetPreparationSummary(BaseModel):
    recorded: bool
    summary: dict[str, int] | None
    issues: int


class TrainingDatasetMediaObservations(BaseModel):
    verified: int
    unavailable: int
    unchecked: int
    last_checked_at: datetime | None


class TrainingDatasetInsightOverview(BaseModel):
    version_id: uuid.UUID
    schema_version: int
    input_digest: str
    snapshot_digest: str
    manifest_digest: str
    status: Literal["complete", "partial", "unavailable"]
    task: Literal["detect", "segment"] | None = "detect"
    counts: dict[str, int]
    splits: dict[str, dict[str, int]]
    classes_total: int
    dimensions: dict[str, int | None]
    dimension_bins: list[TrainingDatasetDistributionBin]
    box_area_bins: list[TrainingDatasetDistributionBin]
    preparation: TrainingDatasetPreparationSummary
    observations: dict[str, int]
    validation: TrainingDatasetValidationIdentity
    media: TrainingDatasetMediaObservations


class TrainingDatasetClassStatistics(BaseModel):
    index: int
    name: str
    train_images: int
    val_images: int
    train_instances: int
    val_instances: int


class TrainingDatasetClassPage(BaseModel):
    items: list[TrainingDatasetClassStatistics]
    total: int
    limit: int
    offset: int


class TrainingDatasetAnalyzedSampleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
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


class TrainingDatasetSamplePage(BaseModel):
    source_version_id: uuid.UUID
    items: list[TrainingDatasetAnalyzedSampleOut]
    total: int
    limit: int
    offset: int


class TrainingDatasetNormalizedBox(BaseModel):
    type: Literal["bbox"] = "bbox"
    class_index: int
    class_name: str
    x_center: float
    y_center: float
    width: float
    height: float


class TrainingDatasetNormalizedPolygon(BaseModel):
    type: Literal["polygon"]
    class_index: int
    class_name: str
    points: list[list[float]]
    area: float
    annotation_id: uuid.UUID
    annotation_version: int


class TrainingDatasetSampleDetail(BaseModel):
    sample: TrainingDatasetAnalyzedSampleOut
    labels: list[TrainingDatasetNormalizedBox | TrainingDatasetNormalizedPolygon]
    total: int | None
    limit: int
    offset: int


class TrainingDatasetInsightComparison(BaseModel):
    baseline_id: uuid.UUID
    version_id: uuid.UUID
    status: Literal["complete", "partial", "unavailable"]
    counts: dict[str, int]
    classes_added: int
    classes_removed: int
    class_order_changed: bool
    class_encoding_changed: bool
    semantic_validation_changed: bool | None
    evaluation_benchmark_changed: bool | None
    baseline_validation: TrainingDatasetValidationIdentity
    target_validation: TrainingDatasetValidationIdentity
