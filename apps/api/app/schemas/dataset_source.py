"""Inventory is deliberately separate from consumable dataset samples."""

from datetime import datetime
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class DatasetSourceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dataset_id: uuid.UUID
    location_id: uuid.UUID
    name: str = Field(min_length=1, max_length=100)
    relative_path: str = Field(default="", max_length=1000)
    node_id: str = Field(min_length=1, max_length=248, pattern=r"^[a-zA-Z0-9_.@:-]+$")
    recursive: bool = True
    include_globs: list[str] = Field(
        default_factory=lambda: ["*"], min_length=1, max_length=20
    )
    exclude_globs: list[str] = Field(default_factory=list, max_length=20)
    ownership_mode: Literal["reference", "managed_copy"] = "reference"
    cache_policy: Literal["none", "preview"] = "preview"


class DatasetSourceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=100)
    enabled: bool | None = None
    cache_policy: Literal["none", "preview"] | None = None


class DatasetSourceOut(BaseModel):
    id: uuid.UUID
    dataset_id: uuid.UUID | None
    location_id: uuid.UUID
    name: str
    config_revision: int
    enabled: bool
    mode: str = "inventory"
    relative_path: str
    node_id: str
    recursive: bool
    include_globs: list[str]
    exclude_globs: list[str] = Field(default_factory=list)
    ownership_mode: Literal["reference", "managed_copy"]
    cache_policy: Literal["none", "preview"]
    policy_revision: int
    unregistered_at: datetime | None


class DatasetSourceList(BaseModel):
    items: list[DatasetSourceOut]
    total: int


class DatasetScanOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    source_id: uuid.UUID
    async_job_id: uuid.UUID | None
    status: str
    phase: str
    complete: bool
    generation: int
    error_code: str | None
    counts: dict[str, int]
    created_at: datetime
    completed_at: datetime | None


class DatasetScanList(BaseModel):
    items: list[DatasetScanOut]
    total: int


class DatasetScanEntryOut(BaseModel):
    path_digest: str
    relative_path: str
    kind: str
    state: str
    size_bytes: int | None
    content_verified: bool = False
    registered_item_id: uuid.UUID | None = None
    preview_state: str | None = None
    preview_bytes: int | None = None
    verified_content: dict | None = None


class VerifiedScanContent(BaseModel):
    sha256: str
    size_bytes: int
    media_type: str
    width: int
    height: int


class DatasetScanEntryList(BaseModel):
    items: list[DatasetScanEntryOut]
    total: int


class RegisteredReferenceOut(BaseModel):
    item_id: uuid.UUID
    created: bool
    sha256: str


class DatasetRegistrationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    scan_run_id: uuid.UUID
    source_id: uuid.UUID
    async_job_id: uuid.UUID | None
    phase: str
    status: str
    generation: int
    error_code: str | None
    counts: dict[str, int]
    created_at: datetime
    completed_at: datetime | None
    ownership_mode: str
    cache_policy: str
    source_revision: int


class DatasetRegistrationEntryOut(BaseModel):
    path_digest: str
    relative_path: str
    state: str
    item_id: uuid.UUID | None
    error_code: str | None
    attempts: int
    size_bytes: int | None
    sha256: str | None
    preview_estimated_bytes: int | None = None
    preview_error: str | None = None


class SourceLifecycleImpact(BaseModel):
    source_id: uuid.UUID
    expected_revision: int
    proof: str
    item_count: int
    project_count: int
    task_count: int
    annotation_count: int
    active_attempt_count: int
    preview_count: int
    preview_bytes: int
    measured_at: datetime


class SourceLifecycleChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["pause", "enable", "unregister"]
    expected_revision: int = Field(ge=1)
    impact_proof: str


class DatasetRegistrationEntryList(BaseModel):
    items: list[DatasetRegistrationEntryOut]
    total: int
