"""Inventory is deliberately separate from consumable dataset samples."""

from datetime import datetime
import uuid

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


class DatasetSourceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=100)
    enabled: bool | None = None


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
