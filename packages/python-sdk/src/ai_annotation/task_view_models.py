"""Typed models for project saved views and task queries."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ai_annotation.models import Task, UserBrief, _AAPModel

TaskViewVisibility = Literal["private", "project"]
TaskViewEntityScope = Literal["tasks", "objects", "tracks"]


class _RequestModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        protected_namespaces=("model_validate", "model_dump"),
    )


class TaskSortItem(_RequestModel):
    field: str
    direction: Literal["asc", "desc"] = "asc"


class ProjectTaskViewCreate(_RequestModel):
    name: str = Field(min_length=1, max_length=120)
    visibility: TaskViewVisibility = "private"
    entity_scope: TaskViewEntityScope = "tasks"
    filter_json: dict[str, Any] = Field(default_factory=dict)
    sort_json: list[TaskSortItem] = Field(default_factory=list)
    columns_json: list[str] = Field(default_factory=list)

    @field_validator("columns_json")
    @classmethod
    def strip_empty_columns(cls, value: list[str]) -> list[str]:
        return [item for item in value if item]


class ProjectTaskViewUpdate(_RequestModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    visibility: TaskViewVisibility | None = None
    filter_json: dict[str, Any] | None = None
    sort_json: list[TaskSortItem] | None = None
    columns_json: list[str] | None = None

    @field_validator("columns_json")
    @classmethod
    def strip_empty_columns(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        return [item for item in value if item]


class ProjectTaskViewCopyRequest(_RequestModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    visibility: TaskViewVisibility | None = None


class ProjectTaskView(_AAPModel):
    id: UUID | None = None
    key: str | None = None
    project_id: UUID
    owner_id: UUID | None = None
    name: str
    visibility: TaskViewVisibility = "private"
    entity_scope: TaskViewEntityScope = "tasks"
    filter_json: dict[str, Any] = Field(default_factory=dict)
    sort_json: list[TaskSortItem] = Field(default_factory=list)
    columns_json: list[str] = Field(default_factory=list)
    builtin: bool = False
    task_count: int | None = None
    result_count: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    invalid_fields: list[str] = Field(default_factory=list)


class ProjectTaskViewListResponse(_AAPModel):
    items: list[ProjectTaskView]


class ProjectTaskQueryRequest(_RequestModel):
    filter_json: dict[str, Any] = Field(default_factory=dict)
    sort_json: list[TaskSortItem] = Field(default_factory=list)
    columns_json: list[str] = Field(default_factory=list)
    limit: int = Field(default=50, ge=1, le=200, strict=True)
    offset: int = Field(default=0, ge=0, strict=True)


class DataManagerTask(Task):
    """Task query result with the Data Manager's derived counts and context."""

    effective_assignee: UserBrief | None = None
    effective_reviewer: UserBrief | None = None
    annotation_count: int = 0
    prediction_count: int = 0
    avg_prediction_confidence: float | None = None
    unresolved_issue_count: int = 0
    unresolved_feedback_count: int = 0
    comment_count: int = 0
    model_versions: list[str] = Field(default_factory=list)
    scene_name: str | None = None
    frame_index: int | None = None
    last_activity_at: datetime | None = None
    annotation_source_counts: dict[str, int] = Field(default_factory=dict)
    track_count: int = 0
    pending_prediction_shape_count: int = 0
    low_confidence_prediction_shape_count: int = 0
    pending_tracker_job_count: int = 0
    keyframe_count: int = 0
    outside_range_count: int = 0
    camera_count: int = 0
    calibration_issue_count: int = 0
    scene_total_frames: int | None = None


class ProjectTaskQueryResponse(_AAPModel):
    items: list[DataManagerTask]
    total: int
    limit: int
    offset: int
