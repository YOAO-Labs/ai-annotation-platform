"""Short-lived review of reversible dataset lifecycle changes."""

from datetime import datetime
from typing import Literal
import uuid

from pydantic import BaseModel, ConfigDict, Field

DatasetLifecycleOperation = Literal["archive", "restore"]
DatasetLifecycleState = Literal["active", "archived"]


class DatasetLifecyclePreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: DatasetLifecycleOperation
    expected_revision: int = Field(ge=1)


class DatasetLifecycleApply(DatasetLifecyclePreviewRequest):
    impact_token: str = Field(min_length=1, max_length=4096)


class DatasetLifecycleCounts(BaseModel):
    items: int
    sources: int
    projects: int
    tasks: int
    annotations: int
    active_scans: int
    active_registrations: int
    enabled_schedules: int
    pending_jobs: int
    ready_lineage_versions: int
    unfinished_lineage_versions: int


class DatasetLifecyclePreview(BaseModel):
    dataset_id: uuid.UUID
    lifecycle_revision: int
    state: DatasetLifecycleState
    operation: DatasetLifecycleOperation
    measured_at: datetime
    expires_at: datetime
    impact_token: str
    counts: DatasetLifecycleCounts
    frozen_coverage: Literal["recorded_dataset_membership_only"]


class DatasetLifecycleResult(BaseModel):
    dataset_id: uuid.UUID
    lifecycle_revision: int
    state: DatasetLifecycleState
    archived_at: datetime | None
    archived_by: uuid.UUID | None
