from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from app.schemas.export import ExportRequestBody


MAX_DATA_MANAGER_TASK_IDS = 200


class DataManagerTaskSelection(BaseModel):
    task_ids: list[UUID] = Field(
        ..., min_length=1, max_length=MAX_DATA_MANAGER_TASK_IDS
    )

    @model_validator(mode="after")
    def canonicalize_task_ids(self) -> DataManagerTaskSelection:
        self.task_ids = sorted(set(self.task_ids), key=str)
        return self


class DataManagerTaskAssignmentRequest(DataManagerTaskSelection):
    annotator_id: UUID | None = None
    reviewer_id: UUID | None = None

    @model_validator(mode="after")
    def require_assignment_field(self) -> DataManagerTaskAssignmentRequest:
        if not {"annotator_id", "reviewer_id"}.intersection(self.model_fields_set):
            raise ValueError("annotator_id or reviewer_id required")
        return self


class DataManagerTaskAssignmentApplyRequest(DataManagerTaskAssignmentRequest):
    preview_version: str = Field(min_length=64, max_length=64)


class DataManagerTaskAssignmentItem(BaseModel):
    task_id: UUID
    task_display_id: str | None = None
    batch_id: UUID | None = None
    status: str | None = None
    task_updated_at: datetime | None = None
    before_annotator_id: UUID | None = None
    after_annotator_id: UUID | None = None
    before_reviewer_id: UUID | None = None
    after_reviewer_id: UUID | None = None
    # Raw task columns remain available for compatibility; these fields show
    # the effective owner after a NULL task override falls back to its batch.
    effective_before_annotator_id: UUID | None = None
    effective_after_annotator_id: UUID | None = None
    effective_before_reviewer_id: UUID | None = None
    effective_after_reviewer_id: UUID | None = None
    will_change: bool = False
    reason: str | None = None


class DataManagerTaskAssignmentResponse(BaseModel):
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
