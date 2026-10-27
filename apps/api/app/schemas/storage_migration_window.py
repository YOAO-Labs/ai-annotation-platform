"""Registered-access windows do not certify physical source quiescence."""

from datetime import datetime
from typing import Literal
import uuid

from pydantic import BaseModel, ConfigDict, Field


class StorageMigrationWindowRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    parent_copy_id: uuid.UUID


class StorageMigrationWindowTransition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["stop", "close"]
    expected_revision: int = Field(ge=1, strict=True)


class StorageMigrationFinalizationItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    finalization_id: uuid.UUID
    revision: int
    state: Literal["bound", "sealing", "sealed", "invalidating", "invalidated"]
    window_revision: int
    created_at: datetime
    updated_at: datetime
    native_observed_at: datetime | None = None
    sealed_at: datetime | None = None
    invalidated_at: datetime | None = None
    inventory_job_id: uuid.UUID | None = None
    copy_id: uuid.UUID | None = None
    counts: dict[str, int] | None = None
    error_code: str | None = None
    migration_ready: Literal[False] = False


class StorageMigrationWindowItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    window_id: uuid.UUID
    parent_copy_id: uuid.UUID
    target_location_id: uuid.UUID
    state: Literal["draining", "stopped", "closing", "closed"]
    revision: int
    source_gate_revision: int
    source_config_revision: int
    created_at: datetime
    updated_at: datetime
    created_by: uuid.UUID | None = None
    registered_operations: int = Field(ge=0)
    registered_access_drained: bool
    source_quiescence_proven: Literal[False] = False
    migration_ready: Literal[False] = False
    finalization: StorageMigrationFinalizationItem | None = None


class StorageMigrationWindowHistory(BaseModel):
    items: list[StorageMigrationWindowItem]
    total: int
    limit: int
    offset: int
