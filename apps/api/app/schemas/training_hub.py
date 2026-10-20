"""Read-only training entry points, scoped to current project management."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, JsonValue, AliasChoices

from app.schemas.training_run import TrainingRunOut


class TrainingInstanceRleCapability(BaseModel):
    bridge: Literal["ultralytics-instance-rle-1"]
    framework: Literal["ultralytics"]
    version: str
    models: list[str]
    limits: dict[str, int]
    rasterizer: str
    transform: str
    loss_mask_resize: str
    validation_mask_resize: str


class TrainingCapabilities(BaseModel):
    tasks: list[str] = Field(validation_alias=AliasChoices("tasks", "task_types"))
    resume: bool
    artifact_download: bool
    model_registration: bool
    instance_rle: TrainingInstanceRleCapability | None = None


class TrainingBaseModel(BaseModel):
    id: str
    sha256: str
    filename: str
    task: Literal["detect", "segment"] = "detect"
    model_family: str | None = None
    model_size: str | None = None
    object_key: str | None = None
    size_bytes: int | None = Field(default=None, gt=0, le=2 * 1024**3)
    compatible: bool | None = None
    unavailable_reason: str | None = None
    capabilities: TrainingCapabilities | None = None


class TrainingSchemaIdentity(BaseModel):
    schema_id: str
    schema_digest: str
    framework: str
    framework_version: str
    adapter_revision: int
    task: str
    model_family: str


class TrainingSchemaSummary(TrainingSchemaIdentity):
    field_count: int
    editable_count: int
    compatible: bool | None = None


class TrainingParameterField(BaseModel):
    key: str
    label: str
    group: str
    status: Literal["editable", "platform-managed", "inapplicable", "unqualified"]
    native_type: str
    default: JsonValue
    native_default: JsonValue
    defined_in: str
    reason: str
    rules: dict[str, JsonValue]
    aliases: list[str]


class TrainingParameterSchema(TrainingSchemaIdentity):
    source: str
    source_sha256: str
    groups: dict[str, str]
    fields: list[TrainingParameterField]


class TrainingTemplate(BaseModel):
    framework: Literal["ultralytics", "libreyolo"]
    version: str
    image_digest: str
    base_models: list[TrainingBaseModel]
    capabilities: TrainingCapabilities
    compatible: bool | None = None
    parameter_schemas: list[TrainingSchemaSummary] = Field(default_factory=list)


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
