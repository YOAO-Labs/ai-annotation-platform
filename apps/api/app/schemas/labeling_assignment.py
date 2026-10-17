from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field, model_validator


MAX_LABELING_ASSIGNMENT_TASK_IDS = 200


class DataManagerTaskAssignmentRequest(BaseModel):
    task_ids: list[UUID] = Field(
        ..., min_length=1, max_length=MAX_LABELING_ASSIGNMENT_TASK_IDS
    )
    annotator_id: UUID | None = None
    reviewer_id: UUID | None = None

    @model_validator(mode="after")
    def canonicalize_task_ids(self) -> DataManagerTaskAssignmentRequest:
        self.task_ids = sorted(set(self.task_ids), key=str)
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
    # Keep raw task overrides separate from their effective batch fallbacks.
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
