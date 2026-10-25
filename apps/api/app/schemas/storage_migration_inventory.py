"""Fixed-source, locator-free inventory task contracts."""

from datetime import datetime
from typing import Literal
import uuid

from pydantic import BaseModel, ConfigDict, Field

Purpose = Literal[
    "annotations",
    "datasets",
    "bug-reports",
    "media-cache",
    "audit-archive",
    "import",
    "export",
    "avatars",
    "training",
]


class StorageMigrationInventoryLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_pages: int = Field(1000, ge=1, le=100000, strict=True)
    max_entries: int = Field(10000, ge=1, le=100000, strict=True)
    max_object_bytes: int = Field(256 * 1024**2, ge=1, le=1024**3, strict=True)
    max_total_bytes: int = Field(2 * 1024**3, ge=1, le=16 * 1024**3, strict=True)
    max_seconds: int = Field(600, ge=1, le=3600, strict=True)
    read_timeout_seconds: int = Field(30, ge=1, le=60, strict=True)
    page_size: int = Field(1000, ge=1, le=1000, strict=True)


class StorageMigrationInventoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limits: StorageMigrationInventoryLimits = Field(
        default_factory=StorageMigrationInventoryLimits
    )
    handling: dict[Purpose, Literal["migrate", "rebuild", "retain"]] = Field(
        default_factory=dict
    )


class StorageMigrationInventoryCounts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    buckets: int = Field(ge=0, strict=True)
    objects: int = Field(ge=0, strict=True)
    delete_markers: int = Field(ge=0, strict=True)
    multipart: int = Field(ge=0, strict=True)
    verified_objects: int = Field(ge=0, strict=True)
    verified_bytes: int = Field(ge=0, strict=True)
    bytes_read: int = Field(ge=0, strict=True)
    pages: int = Field(ge=0, strict=True)
    issues: int = Field(ge=0, strict=True)
    entries: int = Field(ge=0, strict=True)


class StorageMigrationInventoryItem(BaseModel):
    job_id: uuid.UUID
    status: Literal["pending", "running", "completed", "failed", "cancelled"]
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    scope: Literal["selected-configured-source"] = "selected-configured-source"
    source_location_id: uuid.UUID
    migration_ready: Literal[False] = False
    limits: StorageMigrationInventoryLimits
    handling: dict[Purpose, Literal["migrate", "rebuild", "retain", "unreviewed"]]
    cancel_requested: bool
    complete: bool | None
    counts: StorageMigrationInventoryCounts | None
    error_code: str | None
    report_available: bool
    report_sha256: str | None
    report_size_bytes: int | None


class StorageMigrationInventoryStart(BaseModel):
    item: StorageMigrationInventoryItem
    reused: bool


class StorageMigrationInventoryHistory(BaseModel):
    items: list[StorageMigrationInventoryItem]
    total: int
    limit: int
    offset: int
