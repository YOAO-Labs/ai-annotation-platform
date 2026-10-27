"""Closed public migration-copy DTOs; locators remain private attachments."""

from datetime import datetime
from typing import Annotated, Literal
import uuid

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.storage_migration_inventory import Purpose


class StorageMigrationCopyLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_object_bytes: int = Field(256 * 1024**2, ge=1, le=1024**3, strict=True)
    max_source_bytes: int = Field(2 * 1024**3, ge=1, le=16 * 1024**3, strict=True)
    max_upload_bytes: int = Field(2 * 1024**3, ge=1, le=16 * 1024**3, strict=True)
    max_verify_bytes: int = Field(2 * 1024**3, ge=1, le=16 * 1024**3, strict=True)
    max_objects: int = Field(10000, ge=1, le=100000, strict=True)
    max_seconds: int = Field(1800, ge=1, le=3600, strict=True)
    connect_timeout_seconds: int = Field(10, ge=1, le=60, strict=True)
    read_timeout_seconds: int = Field(60, ge=1, le=60, strict=True)


class StorageMigrationCopyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    inventory_job_id: uuid.UUID
    target_location_id: uuid.UUID
    handling: dict[Purpose, Literal["migrate", "rebuild", "retain"]]
    limits: StorageMigrationCopyLimits = Field(
        default_factory=StorageMigrationCopyLimits
    )

    @model_validator(mode="after")
    def all_purposes(self):
        from app.services.storage_migration_inventory import PURPOSES

        if set(self.handling) != PURPOSES:
            raise ValueError("All nine purposes must be reviewed")
        return self


class StorageMigrationCopyAttemptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: Literal["prepare", "run", "verify", "recover"]
    retry_failed: bool = Field(False, strict=True)
    retry_object_ids: list[Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]] | None = (
        Field(None, min_length=1, max_length=1000)
    )

    @model_validator(mode="after")
    def selection(self):
        if self.retry_failed and self.operation != "run":
            raise ValueError("Only run accepts retry_failed")
        if self.retry_object_ids is not None and (
            self.operation != "run"
            or not self.retry_failed
            or len(set(self.retry_object_ids)) != len(self.retry_object_ids)
        ):
            raise ValueError("Selected retries require unique failed objects")
        return self


class StorageMigrationCopyGenerationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    inventory_job_id: uuid.UUID


class StorageMigrationCopyGenerationSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reused_objects: int = Field(ge=0, strict=True)
    new_objects: int = Field(ge=0, strict=True)
    mutable_recopies: int = Field(ge=0, strict=True)
    source_identity_conflicts: int = Field(ge=0, strict=True)
    previous_only_objects: int = Field(ge=0, strict=True)
    delete_markers: int = Field(ge=0, strict=True)
    history_records: int = Field(ge=0, strict=True)


class StorageMigrationCopyCounts(BaseModel):
    model_config = ConfigDict(extra="forbid")
    verified: int = Field(ge=0, strict=True)
    pending: int = Field(ge=0, strict=True)
    writing: int = Field(ge=0, strict=True)
    uncertain: int = Field(ge=0, strict=True)
    failed: int = Field(ge=0, strict=True)
    skipped: int = Field(ge=0, strict=True)


class StorageMigrationCopyCatalogSummary(BaseModel):
    complete: bool
    enumeration_complete: bool
    resolution_complete: bool
    counts: dict[str, int]


class StorageMigrationCopyItem(BaseModel):
    copy_id: uuid.UUID
    window_id: uuid.UUID | None = None
    window_revision: int | None = None
    parent_copy_id: uuid.UUID | None = None
    generation: int = Field(1, ge=1)
    successor_copy_id: uuid.UUID | None = None
    parent_evidence_sha256: str | None = None
    read_only: bool = False
    generation_summary: StorageMigrationCopyGenerationSummary | None = None
    created_at: datetime
    updated_at: datetime
    source_inventory_job_id: uuid.UUID | None
    source_report_sha256: str
    source_complete: bool
    target_location_id: uuid.UUID
    target_name: str | None
    handling: dict[Purpose, Literal["migrate", "rebuild", "retain"]]
    limits: StorageMigrationCopyLimits
    prepared: bool
    job_id: uuid.UUID | None
    operation: Literal["prepare", "run", "verify"] | None
    status: Literal["pending", "running", "completed", "failed", "cancelled"] | None
    cancel_requested: bool
    counts: StorageMigrationCopyCounts | None
    decisions: dict[str, int] | None
    source_summary: dict[str, int | bool] | None
    catalog_summary: StorageMigrationCopyCatalogSummary | None
    copy_subset_complete: bool | None
    migration_ready: Literal[False] = False
    error_code: str | None
    io: dict[str, int] | None
    checkpoint_available: bool
    attempt_subset_complete: bool | None
    selected_count: int | None
    selected_verified_count: int | None


class StorageMigrationCopyStart(BaseModel):
    item: StorageMigrationCopyItem
    reused: bool


class StorageMigrationCopyHistory(BaseModel):
    items: list[StorageMigrationCopyItem]
    total: int
    limit: int
    offset: int


class StorageMigrationCopyTarget(BaseModel):
    location_id: uuid.UUID
    name: str
    config_revision: int


class StorageMigrationCopyTargets(BaseModel):
    items: list[StorageMigrationCopyTarget]
    total: int
    limit: int
    offset: int


class StorageMigrationCopyFailedObject(BaseModel):
    object_id: str
    size_bytes: int
    error_code: str


class StorageMigrationCopyFailedObjects(BaseModel):
    items: list[StorageMigrationCopyFailedObject]
    total: int
    limit: int
    offset: int
