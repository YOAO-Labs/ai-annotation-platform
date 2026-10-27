"""Reviewed, location-scoped maintenance admission controls."""

from datetime import datetime
from typing import Literal
import uuid

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.storage_location import StorageMaintenanceImpact

StorageMaintenanceOperation = Literal["drain", "stop", "resume"]
StorageMaintenanceState = Literal["active", "draining", "stopped"]


class StorageMaintenanceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: StorageMaintenanceOperation
    expected_revision: int = Field(ge=1)


class StorageMaintenanceApply(StorageMaintenanceRequest):
    impact_token: str = Field(min_length=1, max_length=4096)


class StorageMaintenanceOpenOperation(BaseModel):
    id: uuid.UUID
    purpose: str
    node_id: str
    revision: int
    started_at: datetime


class StorageMaintenanceStatus(BaseModel):
    location_id: uuid.UUID
    state: StorageMaintenanceState
    revision: int
    changed_at: datetime | None
    changed_by: uuid.UUID | None
    measured_at: datetime
    protocol_enabled: bool
    open_operations: int
    operations: list[StorageMaintenanceOpenOperation] = Field(max_length=50)
    has_more: bool
    coverage: Literal["registered_location_access_only"]
    physical_cleanup_permission: Literal[False]


class StorageMaintenancePreview(StorageMaintenanceStatus):
    operation: StorageMaintenanceOperation
    target_state: StorageMaintenanceState
    expires_at: datetime
    impact_token: str
    impact: StorageMaintenanceImpact
