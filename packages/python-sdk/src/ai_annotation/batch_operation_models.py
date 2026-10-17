"""Typed request and response models for batch operations beyond CRUD."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ai_annotation.export_models import (
    LidarExportPreflightRequest as LidarExportPreflightRequest,
    LidarExportIssue as LidarExportIssue,
    LidarExportPreflightResult as LidarExportPreflightResult,
)
from ai_annotation.models import _AAPModel


class _BatchRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        allow_inf_nan=False,
        protected_namespaces=("model_validate", "model_dump"),
    )


class UnclassifiedTaskCount(_AAPModel):
    count: int


class BatchAuditLogEntry(_AAPModel):
    id: int
    created_at: datetime | None = None
    actor_id: str | None = None
    actor_email: str | None = None
    actor_role: str | None = None
    action: str
    target_type: str | None = None
    target_id: str | None = None
    detail: dict[str, Any] | None = None


class BatchSubmitReviewResult(_AAPModel):
    batch_id: UUID
    status: str
    submitted_tasks: int = 0
    skipped_tasks: int = 0
    remaining_tasks: int = 0


class BatchIdsRequest(_BatchRequest):
    batch_ids: list[UUID] = Field(min_length=1, max_length=200)


class BatchSplitRequest(_BatchRequest):
    strategy: Literal["metadata", "id_range", "random", "by_scene"]
    metadata_key: str | None = None
    metadata_value: str | None = None
    item_ids: list[UUID] | None = None
    n_batches: int | None = Field(default=None, ge=1, le=100)
    shuffle: bool = True
    name_prefix: str = "Batch"
    priority: int = Field(default=50, ge=0, le=100)
    deadline: date | None = None
    annotator_id: UUID | None = None
    reviewer_id: UUID | None = None


class BatchDistributionRequest(_BatchRequest):
    batch_ids: list[UUID] | None = Field(default=None, min_length=1, max_length=500)
    annotator_ids: list[UUID] = Field(default_factory=list)
    reviewer_ids: list[UUID] = Field(default_factory=list)
    only_unassigned: bool = True

    @model_validator(mode="after")
    def require_recipients(self) -> BatchDistributionRequest:
        if not self.annotator_ids and not self.reviewer_ids:
            raise ValueError("annotator_ids or reviewer_ids is required")
        return self


class ReviewedBatchDistribution(BatchDistributionRequest):
    preview_version: str = Field(min_length=64, max_length=64)


class BatchAssignmentPreviewRequest(_BatchRequest):
    # These fields are required, nullable targets. Explicit null means clear that role.
    annotator_id: UUID | None
    reviewer_id: UUID | None


class ReviewedBatchAssignment(BatchAssignmentPreviewRequest):
    preview_version: str = Field(min_length=64, max_length=64)


class BatchDistributionPreviewItem(_AAPModel):
    batch_id: UUID
    display_id: str
    name: str
    status: str
    task_count: int = 0
    before_annotator_id: UUID | None = None
    after_annotator_id: UUID | None = None
    before_reviewer_id: UUID | None = None
    after_reviewer_id: UUID | None = None
    will_change: bool
    skipped_reason: str | None = None


class BatchDistributionRecipient(_AAPModel):
    user_id: UUID
    role: Literal["annotator", "reviewer"]
    new_task_count: int
    existing_backlog_count: int


class BatchDistributionPreview(_AAPModel):
    project_id: UUID
    only_unassigned: bool
    total_batches: int
    candidate_batches: int
    changed_batches: int
    skipped_batches: int
    items: list[BatchDistributionPreviewItem]
    recipient_summary: list[BatchDistributionRecipient]
    preview_version: str


class AdminBatchLockRequest(_BatchRequest):
    reason: str = Field(min_length=1, max_length=500)
