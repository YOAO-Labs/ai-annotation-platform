"""Bounded reads over frozen training data and derived metadata."""

import uuid
from datetime import datetime
from typing import Annotated, Literal

from aap_task_contract import VisualTask
from pydantic import BaseModel, ConfigDict, Field
from app.schemas._jsonb_types import CocoRleContent


class TrainingDatasetFrozenMask(BaseModel):
    annotation_id: uuid.UUID
    annotation_version: int = Field(ge=1)
    pixel_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    rle: CocoRleContent


class TrainingDatasetInsightCapability(BaseModel):
    supported: bool
    reason: str | None = None


class TrainingDatasetInsightCapabilities(BaseModel):
    create_version: TrainingDatasetInsightCapability
    qualified_training: TrainingDatasetInsightCapability
    overview_statistics: TrainingDatasetInsightCapability
    task_statistics: TrainingDatasetInsightCapability
    sample_preview: TrainingDatasetInsightCapability
    semantic_comparison: TrainingDatasetInsightCapability


class TrainingDatasetInsightContractProfile(BaseModel):
    task: VisualTask
    media_type: str
    format: str
    geometry: str
    bridge: str


class TrainingDatasetInsightAdapter(BaseModel):
    id: str
    revision: int


class TrainingDatasetInsightEvidence(BaseModel):
    analyzed_members: int
    unknown_label_members: int
    known_instances: int
    geometry_unknown_members: int


class TrainingDatasetStatisticAxisBin(BaseModel):
    key: str
    label: str
    lower: float | None
    upper: float | None


class TrainingDatasetStatisticHeatmap(BaseModel):
    x_bins: list[TrainingDatasetStatisticAxisBin]
    y_bins: list[TrainingDatasetStatisticAxisBin]
    counts: list[list[int]]
    unit: Literal["member_images", "instances"]
    population: int
    unknown_members: int


class TrainingDatasetObjectDensityBin(BaseModel):
    key: str
    label: str
    count: int


class TrainingDatasetObjectDensity(BaseModel):
    bins: list[TrainingDatasetObjectDensityBin]
    analyzed_members: int
    unknown_label_members: int


class TrainingDatasetDetectionStatistics(BaseModel):
    task: Literal["detect"] = "detect"
    schema_version: Literal[1] = 1
    image_dimensions: TrainingDatasetStatisticHeatmap
    box_centers: TrainingDatasetStatisticHeatmap
    box_dimensions: TrainingDatasetStatisticHeatmap
    objects_per_member: TrainingDatasetObjectDensity


class TrainingDatasetSegmentationStatistics(BaseModel):
    task: Literal["segment"] = "segment"
    schema_version: Literal[1] = 1
    geometry: Literal["normalized-simple-polygon", "instance-mask"] = (
        "normalized-simple-polygon"
    )
    known_instances: int
    polygon_area_sum: float
    mask_area_pixels_sum: int | None = None
    normalized_mask_area_sum: float | None = None


TrainingDatasetTaskStatistics = Annotated[
    TrainingDatasetDetectionStatistics | TrainingDatasetSegmentationStatistics,
    Field(discriminator="task"),
]


class TrainingDatasetDistributionBin(BaseModel):
    key: str
    label: str
    train: int
    val: int


class TrainingDatasetComparisonValidationIdentity(BaseModel):
    schema_version: Literal[1] = 1
    contract_profile_digest: str
    semantic_digest: str | None
    evaluation_multiset_digest: str | None
    ordered_classes_digest: str


class TrainingDatasetValidationIdentity(BaseModel):
    manifest_digest: str | None
    semantic_digest: str | None
    evaluation_multiset_digest: str | None
    ordered_classes_digest: str
    raw_contract_digest: str | None = None
    comparison_identity: TrainingDatasetComparisonValidationIdentity | None = None


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
    task: VisualTask | None = "detect"
    contract_profile: TrainingDatasetInsightContractProfile | None
    adapter: TrainingDatasetInsightAdapter | None
    capabilities: TrainingDatasetInsightCapabilities
    evidence: TrainingDatasetInsightEvidence
    task_statistics: TrainingDatasetTaskStatistics | None
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
    order_by: Literal["index", "instances", "images"] = "index"
    split: Literal["train", "val"] | None = None


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


class TrainingDatasetInstanceMask(BaseModel):
    type: Literal["instance_mask"] = "instance_mask"
    class_index: int
    class_name: str
    annotation_id: uuid.UUID
    annotation_version: int
    size: list[int]
    area: float
    area_pixels: int
    bbox_xyxy: list[int]
    pixel_sha256: str


class TrainingDatasetFrozenSourceLineage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: uuid.UUID
    location_id: uuid.UUID
    path_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    scan_run_id: uuid.UUID
    accepted_revision_id: uuid.UUID | None
    revision_number: int | None
    previous_revision_id: uuid.UUID | None
    accepted_at: datetime | None
    scan_completed_at: datetime | None
    content_verified_at: datetime | None
    source_config_revision: int
    location_config_revision: int


class TrainingDatasetFrozenManagedSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = Field(alias="schema")
    owner_kind: Literal["dataset_asset", "task_original"]
    content_id: uuid.UUID
    location_id: uuid.UUID
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    size_bytes: int = Field(gt=0)


class TrainingDatasetSourceLineageEvidence(BaseModel):
    kind: Literal["accepted_reference", "managed_original", "unknown"] = "unknown"
    status: Literal["recorded", "partial", "unrecorded"]
    reasons: list[str]
    evidence: TrainingDatasetFrozenSourceLineage | None
    managed_evidence: TrainingDatasetFrozenManagedSource | None = None


class TrainingDatasetSampleDetail(BaseModel):
    sample: TrainingDatasetAnalyzedSampleOut
    labels: list[
        TrainingDatasetNormalizedBox
        | TrainingDatasetNormalizedPolygon
        | TrainingDatasetInstanceMask
    ]
    total: int | None
    limit: int
    offset: int
    source_lineage: TrainingDatasetSourceLineageEvidence | None = None


class TrainingDatasetInsightComparison(BaseModel):
    baseline_id: uuid.UUID
    version_id: uuid.UUID
    status: Literal["complete", "partial", "unavailable"]
    counts: dict[str, int]
    count_unit: Literal["unique_contents"]
    source_comparison_status: Literal["complete", "partial", "unavailable"] = (
        "unavailable"
    )
    baseline_members: int
    target_members: int
    overlapping_changes: bool
    semantic_unchanged_label: str
    classes_added: int
    classes_removed: int
    class_order_changed: bool
    class_encoding_changed: bool
    semantic_validation_changed: bool | None
    evaluation_benchmark_changed: bool | None
    validation_comparison_basis: Literal["canonical-profile-validation-1"]
    raw_contract_changed: bool | None
    baseline_validation: TrainingDatasetValidationIdentity
    target_validation: TrainingDatasetValidationIdentity
