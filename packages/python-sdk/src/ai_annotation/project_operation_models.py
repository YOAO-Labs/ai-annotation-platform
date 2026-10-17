"""Typed models for project query, maintenance, and discovery operations."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator

from ai_annotation.export_models import (
    LidarExportPreflightRequest as LidarExportPreflightRequest,
    LidarExportIssue as LidarExportIssue,
    LidarExportPreflightResult,
)

from ai_annotation.models import Project, _AAPModel

LidarExportPreflight = LidarExportPreflightResult


class ProjectPage(_AAPModel):
    items: list[Project]
    total: int
    page: int
    page_size: int
    pages: int


class ProjectReadiness(_AAPModel):
    project_id: UUID
    guide_ready: bool
    classes_ready: bool
    linked_dataset_count: int
    dataset_item_count: int
    task_count: int
    linked_task_count: int
    task_creation_active_jobs: int = 0
    task_creation_failed_jobs: int = 0
    latest_task_creation_status: str | None = None
    batch_count: int
    nonempty_batch_count: int
    executable_batch_count: int
    assigned_batch_count: int
    active_annotator_count: int
    active_reviewer_count: int
    activated_batch_count: int


class ProjectClassUsage(_AAPModel):
    classes: dict[str, int] = Field(default_factory=dict)
    attributes: dict[str, int] = Field(default_factory=dict)


class ProjectClassRenameRequest(_AAPModel):
    model_config = ConfigDict(extra="forbid")

    old_name: str
    new_name: str
    tool_unit_id: str | None = None

    @field_validator("old_name", "new_name")
    @classmethod
    def require_nonblank_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("class name must not be blank")
        return normalized


class ProjectCleanupOrphansRequest(_AAPModel):
    model_config = ConfigDict(extra="forbid")

    dry_run: bool = True


class ProjectCleanupOrphansResult(_AAPModel):
    orphan_annotations: int = 0
    orphan_attribute_keys: dict[str, int] = Field(default_factory=dict)


class ProjectDataset(_AAPModel):
    id: UUID
    display_id: str
    name: str
    data_type: str
    linked_at: datetime | None = None
    items_count: int
    tasks_in_project: int


class ProjectMentionCandidate(_AAPModel):
    user_id: UUID
    user_name: str
    user_email: str | None = None
    kind: Literal["member", "owner", "super_admin"] = "member"


class ProjectTransferRequest(_AAPModel):
    model_config = ConfigDict(extra="forbid")

    new_owner_id: UUID


class OrphanTasksPreview(_AAPModel):
    orphan_tasks: int
    orphan_annotations: int


class OrphanTasksCleanupResult(_AAPModel):
    deleted_tasks: int
    deleted_annotations: int
