"""Typed contracts for project Data Manager discovery, queries, and actions."""

from __future__ import annotations
from datetime import datetime
from typing import Any, Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, model_validator
from ai_annotation.models import _AAPModel
from ai_annotation.export_models import ExportRequestBody


class _RequestModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        allow_inf_nan=False,
        protected_namespaces=("model_validate", "model_dump"),
    )


DataManagerValueType = Literal[
    "text", "number", "boolean", "select", "multiselect", "datetime"
]
DataManagerEntityScope = Literal["tasks", "objects", "tracks"]


class DataManagerProjectKindOut(_AAPModel):
    data_type: str
    type_key: str
    scene_mode: bool


class DataManagerOptionOut(_AAPModel):
    value: str
    label: str


class DataManagerFilterFieldOut(_AAPModel):
    key: str
    label: str
    group: str
    value_type: DataManagerValueType
    operators: list[str]
    options: list[DataManagerOptionOut] = Field(default_factory=list)
    expensive: bool = False
    tool_unit_id: str | None = None
    attribute_key: str | None = None


class DataManagerColumnOut(_AAPModel):
    key: str
    label: str
    group: str
    default: bool = False
    expensive: bool = False
    sortable: bool = False
    sort_field: str | None = None


class DataManagerMetricOut(_AAPModel):
    key: str
    label: str
    group: str


class DataManagerToolUnitOut(_AAPModel):
    id: str
    classes: list[str] = Field(default_factory=list)


class DataManagerSchemaResponse(_AAPModel):
    entity_scope: DataManagerEntityScope = "tasks"
    available_entity_scopes: list[DataManagerEntityScope] = Field(
        default_factory=lambda: ["tasks", "objects"]
    )
    project_kind: DataManagerProjectKindOut
    tool_units: list[DataManagerToolUnitOut] = Field(default_factory=list)
    filter_fields: list[DataManagerFilterFieldOut] = Field(default_factory=list)
    columns: list[DataManagerColumnOut] = Field(default_factory=list)
    default_columns: list[str] = Field(default_factory=list)
    sort_fields: list[DataManagerOptionOut] = Field(default_factory=list)
    metrics: list[DataManagerMetricOut] = Field(default_factory=list)
    builtin_views: list[str] = Field(default_factory=list)


class DataManagerSummaryRequest(_RequestModel):
    filter_json: dict[str, Any] = Field(
        default_factory=dict,
        description="Filter tree qualified by the current project and entity schema; preserve its grouping and dynamic keys.",
    )
    dimensions: list[str] = Field(
        default_factory=list,
        description="Accepted for compatibility; the current API returns its standard aggregate set.",
    )


class DataManagerScopeSummary(_AAPModel):
    visible_task_total: int = 0
    matched_task_total: int = 0


class DataManagerAnnotationSummary(_AAPModel):
    total: int = 0
    single_frame: int = 0
    tracked: int = 0
    distinct_tracks: int = 0
    imported: int = 0
    by_source: dict[str, int] = Field(default_factory=dict)
    by_class: dict[str, int] = Field(default_factory=dict)
    by_tool_unit: dict[str, int] = Field(default_factory=dict)
    by_type: dict[str, int] = Field(default_factory=dict)


class DataManagerAiReviewSummary(_AAPModel):
    prediction_shapes: int = 0
    low_confidence_prediction_shapes: int = 0
    tracker_jobs: int = 0
    confidence_threshold: float = 0.5
    by_model_version: dict[str, int] = Field(default_factory=dict)
    confidence_buckets: dict[str, int] = Field(default_factory=dict)


class DataManagerAttributeSummary(_AAPModel):
    tool_unit_id: str
    key: str
    label: str
    eligible: int = 0
    present: int = 0
    missing: int = 0
    values: dict[str, int] = Field(default_factory=dict)


class DataManagerSummaryResponse(_AAPModel):
    scope: DataManagerScopeSummary
    task_status: dict[str, int] = Field(default_factory=dict)
    annotations: DataManagerAnnotationSummary
    ai_review: DataManagerAiReviewSummary
    unresolved_feedback: int = 0
    attributes: list[DataManagerAttributeSummary] = Field(default_factory=list)
    kind_metrics: dict[str, int | float | None] = Field(default_factory=dict)


class DataManagerMatchesRequest(_RequestModel):
    filter_json: dict[str, Any] = Field(
        default_factory=dict,
        description="Filter tree qualified by the current project and entity schema; preserve its grouping and dynamic keys.",
    )
    limit: int = Field(default=100, ge=1, le=200)
    offset: int = Field(default=0, ge=0)


class DataManagerMatchItem(_AAPModel):
    entity_kind: Literal["annotation", "prediction_shape", "tracker_job"]
    id: UUID
    shape_index: int | None = None
    track_id: str | None = None
    class_name: str | None = None
    tool_unit_id: str | None = None
    annotation_type: str | None = None
    source: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)
    frame_index: int | None = None


class DataManagerMatchesResponse(_AAPModel):
    task_id: UUID
    items: list[DataManagerMatchItem] = Field(default_factory=list)
    total: int = 0
    limit: int
    offset: int


class DataManagerEntityQueryRequest(_RequestModel):
    filter_json: dict[str, Any] = Field(
        default_factory=dict,
        description="Filter tree qualified by the current project and entity schema; preserve its grouping and dynamic keys.",
    )
    sort_json: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Current entity queries use the first sort item. Reset cursor when filters or sorting change.",
    )
    columns_json: list[str] = Field(
        default_factory=list,
        description="Validated entity columns; the current API does not prune returned row fields.",
    )
    limit: int = Field(default=50, ge=1, le=200)
    cursor: str | None = Field(
        default=None,
        max_length=2048,
        description="Opaque entity-page cursor; reuse only with unchanged query context.",
    )


class DataManagerEntityLocation(_AAPModel):
    project_id: UUID
    task_id: UUID
    task_display_id: str
    batch_id: UUID | None = None
    dataset_item_id: UUID | None = None
    data_type: str
    focus_kind: Literal["annotation", "track"]
    annotation_id: UUID | None = None
    track_id: str | None = None
    scene_id: UUID | None = None
    scene_name: str | None = None
    scene_frame_index: int | None = None
    video_frame_index: int | None = None


class DataManagerObjectOut(_AAPModel):
    entity_key: str
    annotation_id: UUID
    task_id: UUID
    task_display_id: str
    file_name: str | None = None
    batch_id: UUID | None = None
    class_name: str
    tool_unit_id: str
    annotation_type: str
    source: str
    imported: bool = False
    confidence: float | None = None
    track_id: str | None = None
    parent_prediction_id: UUID | None = None
    parent_annotation_id: UUID | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)
    attribute_origins: dict[str, str] = Field(default_factory=dict)
    created_by_id: UUID | None = None
    created_by_name: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    unresolved_feedback_count: int = 0
    location: DataManagerEntityLocation


class DataManagerEntityFacets(_AAPModel):
    matched_total: int = Field(
        default=0,
        description="Complete matched entity count for the query grain, not current page length.",
    )
    task_total: int = 0
    by_class: dict[str, int] = Field(default_factory=dict)
    by_source: dict[str, int] = Field(default_factory=dict)
    by_tool_unit: dict[str, int] = Field(default_factory=dict)
    by_type: dict[str, int] = Field(default_factory=dict)
    by_quality: dict[str, int] = Field(default_factory=dict)


class DataManagerObjectQueryResponse(_AAPModel):
    items: list[DataManagerObjectOut] = Field(default_factory=list)
    total: int
    limit: int
    next_cursor: str | None = None
    facets: DataManagerEntityFacets = Field(default_factory=DataManagerEntityFacets)


class DataManagerObjectDetailResponse(_AAPModel):
    item: DataManagerObjectOut


class DataManagerTrackSourceSummary(_AAPModel):
    annotation_sources: dict[str, int] = Field(default_factory=dict)
    keyframe_sources: dict[str, int] = Field(default_factory=dict)


class DataManagerTrackOut(_AAPModel):
    entity_key: str
    track_ref: str = Field(
        description="Opaque server locator used by track_detail; distinct from the business track_id."
    )
    track_kind: Literal["compact_video", "scene"]
    track_id: str = Field(
        description="Business logical-track ID; do not substitute this for the encoded track_ref."
    )
    compact_annotation_id: UUID | None = None
    class_name: str | None = None
    tool_unit_id: str | None = None
    annotation_type: str | None = None
    start_frame: int | None = None
    end_frame: int | None = None
    span: int | None = None
    occurrence_count: int = 0
    distinct_task_count: int = 0
    distinct_frame_count: int = 0
    missing_frame_count: int = 0
    duplicate_frame_count: int = 0
    keyframe_count: int = 0
    outside_range_count: int = 0
    occluded_count: int = 0
    sources: DataManagerTrackSourceSummary = Field(
        default_factory=DataManagerTrackSourceSummary
    )
    attributes: dict[str, Any] = Field(default_factory=dict)
    attribute_origins: dict[str, str] = Field(default_factory=dict)
    quality_issues: list[str] = Field(default_factory=list)
    location: DataManagerEntityLocation


class DataManagerTrackQueryResponse(_AAPModel):
    items: list[DataManagerTrackOut] = Field(default_factory=list)
    total: int
    limit: int
    next_cursor: str | None = None
    facets: DataManagerEntityFacets = Field(default_factory=DataManagerEntityFacets)


class DataManagerTrackMemberOut(_AAPModel):
    annotation_id: UUID
    task_id: UUID
    task_display_id: str
    class_name: str
    source: str
    frame_index: int | None = None
    keyframe_source: str | None = None
    occluded: bool = False
    outside: bool = False
    attributes: dict[str, Any] = Field(default_factory=dict)
    attribute_origins: dict[str, str] = Field(default_factory=dict)
    location: DataManagerEntityLocation


class DataManagerTrackDetailResponse(_AAPModel):
    track: DataManagerTrackOut
    members: list[DataManagerTrackMemberOut] = Field(
        default_factory=list,
        description="All currently visible members; the API has no member pagination.",
    )


MAX_DATA_MANAGER_TASK_IDS = 200


class DataManagerTaskSelection(_RequestModel):
    task_ids: list[UUID] = Field(
        ...,
        min_length=1,
        max_length=MAX_DATA_MANAGER_TASK_IDS,
        description="Explicit selection of 1–200 task UUIDs; sorted and deduplicated by the request model, never widened to the project.",
    )

    @model_validator(mode="after")
    def canonicalize_task_ids(self) -> DataManagerTaskSelection:
        self.task_ids = sorted(set(self.task_ids), key=str)
        return self


class DataManagerTaskAssignmentRequest(DataManagerTaskSelection):
    annotator_id: UUID | None = Field(
        default=None,
        description="Omitted keeps the task override; explicit null clears it and inherits the batch annotator.",
    )
    reviewer_id: UUID | None = Field(
        default=None,
        description="Omitted keeps the task override; explicit null clears it and inherits the batch reviewer.",
    )

    @model_validator(mode="after")
    def require_assignment_field(self) -> DataManagerTaskAssignmentRequest:
        if not {"annotator_id", "reviewer_id"}.intersection(self.model_fields_set):
            raise ValueError("annotator_id or reviewer_id required")
        return self


class DataManagerTaskAssignmentApplyRequest(DataManagerTaskAssignmentRequest):
    preview_version: str = Field(
        min_length=64,
        max_length=64,
        description="Opaque version from preview for the same canonical selection and assignment fields; stale state returns 409.",
    )


class DataManagerTaskAssignmentItem(_AAPModel):
    task_id: UUID
    task_display_id: str | None = None
    batch_id: UUID | None = None
    status: str | None = None
    task_updated_at: datetime | None = None
    before_annotator_id: UUID | None = None
    after_annotator_id: UUID | None = None
    before_reviewer_id: UUID | None = None
    after_reviewer_id: UUID | None = None
    effective_before_annotator_id: UUID | None = None
    effective_after_annotator_id: UUID | None = None
    effective_before_reviewer_id: UUID | None = None
    effective_after_reviewer_id: UUID | None = None
    will_change: bool = False
    reason: str | None = None


class DataManagerTaskAssignmentResponse(_AAPModel):
    task_ids: list[UUID]
    preview_version: str
    eligible_count: int = 0
    skipped_count: int = 0
    failed_count: int = 0
    succeeded: list[UUID] = Field(default_factory=list)
    items: list[DataManagerTaskAssignmentItem] = Field(default_factory=list)


class DataManagerTaskExportRequest(ExportRequestBody, DataManagerTaskSelection):
    targets: list[str] = Field(..., min_length=1, max_length=20)
    include_attributes: bool = True
    video_frame_mode: Literal["keyframes", "all_frames"] = "keyframes"
    axis_frame: Literal["iso", "source"] = "iso"
    indexed_overlap_policy: Literal[
        "error", "z_order", "larger_area", "smaller_area"
    ] = "error"
    video_overlap_policy: Literal["error", "z_order", "larger_area", "smaller_area"] = (
        "error"
    )
    mots_frame_base: Literal[0, 1] = 0

    @model_validator(mode="after")
    def reject_video_scope(self) -> DataManagerTaskExportRequest:
        if self.scope is not None:
            raise ValueError("task-scoped export cannot include a video scope")
        return self


class DataManagerTaskExportSubmission(_AAPModel):
    job_id: UUID
    status: str
