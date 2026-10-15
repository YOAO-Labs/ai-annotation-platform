"""Read-only training entry points, scoped to current project management."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from app.schemas.training_run import TrainingRunOut


class TrainingCapabilities(BaseModel):
    task_types: list[str]
    resume: bool
    artifact_download: bool
    model_registration: bool


class TrainingBaseModel(BaseModel):
    id: str
    sha256: str
    filename: str


class TrainingTemplate(BaseModel):
    framework: Literal["ultralytics", "libreyolo"]
    version: str
    image_digest: str
    base_models: list[TrainingBaseModel]
    capabilities: TrainingCapabilities
    compatible: bool | None = None


class TrainingHubCatalog(BaseModel):
    enabled: bool
    online: bool
    busy: bool
    resource_id: str | None
    executor_id: str | None
    last_seen_at: datetime | None
    items: list[TrainingTemplate]


class TrainingProjectSummary(BaseModel):
    id: uuid.UUID
    name: str
    display_id: str | None
    data_type: str
    status: str
    can_prepare_dataset: bool
    preparation_reason: str | None
    ready_dataset_count: int
    active_run_count: int


class TrainingProjectPage(BaseModel):
    total: int
    items: list[TrainingProjectSummary]


class TrainingQueueItem(BaseModel):
    run: TrainingRunOut
    project_name: str
    project_display_id: str | None


class TrainingQueueCounts(BaseModel):
    active: int
    queued: int
    running: int
    succeeded: int
    failed: int
    cancelled: int


class TrainingQueuePage(BaseModel):
    total: int
    counts: TrainingQueueCounts
    items: list[TrainingQueueItem]
