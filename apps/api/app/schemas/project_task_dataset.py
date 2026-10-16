"""Read-only task stream metadata and contract diagnostics."""

import uuid
from datetime import datetime

from aap_task_contract import VisualTask
from pydantic import BaseModel, Field

from app.schemas.training_dataset import TrainingDatasetSummary


class TaskDatasetCapability(BaseModel):
    supported: bool
    reason: str | None = None


class TaskDatasetCapabilities(BaseModel):
    create_version: TaskDatasetCapability
    qualified_training: TaskDatasetCapability
    overview_statistics: TaskDatasetCapability
    task_statistics: TaskDatasetCapability
    sample_preview: TaskDatasetCapability
    semantic_comparison: TaskDatasetCapability


class ProjectTaskDatasetOut(BaseModel):
    id: uuid.UUID | None
    project_id: uuid.UUID
    task: VisualTask
    status_counts: dict[str, int]
    version_count: int
    pending_membership_count: int
    latest_ready_version: TrainingDatasetSummary | None
    capabilities: TaskDatasetCapabilities


class ProjectTaskDatasetPage(BaseModel):
    items: list[ProjectTaskDatasetOut]
    pending_membership_count: int
    unrecognized_version_count: int


class UnrecognizedTrainingDatasetVersion(BaseModel):
    id: uuid.UUID
    name: str
    status: str
    created_at: datetime
    raw_task: str | None
    error_code: str = "unknown_data_contract"
    reason: str = "冻结任务、几何或桥接契约未被识别；未分配任务数据集"


class UnrecognizedTrainingDatasetVersionPage(BaseModel):
    items: list[UnrecognizedTrainingDatasetVersion]
    total: int = Field(ge=0)
