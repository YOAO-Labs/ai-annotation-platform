from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from app.schemas.data_manager import DataManagerEntityScope
from app.schemas.task import TaskOut
from app.schemas.user import UserBrief


TaskViewVisibility = Literal["private", "project"]


class TaskFilterRule(BaseModel):
    field: str
    op: str
    value: Any = None


class TaskFilterGroup(BaseModel):
    op: Literal["and", "or"] = "and"
    rules: list["TaskFilterNode"] = Field(default_factory=list)


TaskFilterNode = TaskFilterRule | TaskFilterGroup
TaskFilterGroup.model_rebuild()


class TaskSortItem(BaseModel):
    field: str
    direction: Literal["asc", "desc"] = "asc"


class ProjectTaskViewBase(BaseModel):
    entity_scope: DataManagerEntityScope = "tasks"
    name: str = Field(min_length=1, max_length=120)
    visibility: TaskViewVisibility = "private"
    filter_json: dict[str, Any] = Field(default_factory=dict)
    sort_json: list[TaskSortItem] = Field(default_factory=list)
    columns_json: list[str] = Field(default_factory=list)

    @field_validator("columns_json")
    @classmethod
    def strip_empty_columns(cls, value: list[str]) -> list[str]:
        return [item for item in value if item]


class ProjectTaskViewCreate(ProjectTaskViewBase):
    pass


class ProjectTaskViewUpdate(BaseModel):
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


class ProjectTaskViewCopyRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    visibility: TaskViewVisibility | None = None


class ProjectTaskViewOut(ProjectTaskViewBase):
    id: UUID | None = None
    key: str | None = None
    project_id: UUID
    owner_id: UUID | None = None
    builtin: bool = False
    task_count: int | None = None
    result_count: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    invalid_fields: list[str] = Field(default_factory=list)

    class Config:
        from_attributes = True


class ProjectTaskViewListResponse(BaseModel):
    items: list[ProjectTaskViewOut]


class ProjectTaskQueryRequest(BaseModel):
    filter_json: dict[str, Any] = Field(default_factory=dict)
    sort_json: list[TaskSortItem] = Field(default_factory=list)
    columns_json: list[str] = Field(default_factory=list)
    limit: int = Field(default=50, ge=1, le=200)
    offset: int = Field(default=0, ge=0)


class DataManagerTaskOut(TaskOut):
    effective_assignee: UserBrief | None = None
    effective_reviewer: UserBrief | None = None
    annotation_count: int = 0
    prediction_count: int = 0
    avg_prediction_confidence: float | None = None
    unresolved_issue_count: int = 0
    # Legacy alias of ``unresolved_issue_count`` kept for older clients.
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


class ProjectTaskQueryResponse(BaseModel):
    items: list[DataManagerTaskOut]
    total: int
    limit: int
    offset: int
