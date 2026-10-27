"""Manager-only replica publication; physical paths never enter this contract."""

from datetime import datetime
import uuid

from pydantic import BaseModel, ConfigDict, Field


class ReferenceReplicaCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    replica_item_id: uuid.UUID
    priority: int = Field(100, ge=0, le=1000)


class ReferenceReplicaUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    priority: int = Field(ge=0, le=1000)


class ReferenceReplicaCandidate(BaseModel):
    item_id: uuid.UUID
    asset_id: uuid.UUID
    source_id: uuid.UUID
    source_name: str
    location_id: uuid.UUID
    location_name: str
    location_kind: str
    site_id: uuid.UUID | None
    site_name: str | None
    agent_id: uuid.UUID | None
    agent_state: str | None


class ReferenceReplicaOut(BaseModel):
    id: uuid.UUID
    item_id: uuid.UUID
    replica_item_id: uuid.UUID
    replica_asset_id: uuid.UUID
    source_id: uuid.UUID
    source_name: str
    location_id: uuid.UUID
    location_name: str
    location_kind: str
    site_id: uuid.UUID | None
    site_name: str | None
    agent_id: uuid.UUID | None
    agent_state: str | None
    priority: int
    created_at: datetime


class ReferenceReplicaList(BaseModel):
    items: list[ReferenceReplicaOut]
    total: int


class ReferenceReplicaCandidateList(BaseModel):
    items: list[ReferenceReplicaCandidate]
    total: int
