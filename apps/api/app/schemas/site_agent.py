"""Public site identities never disclose tokens, digests or absolute roots."""

from datetime import datetime
from typing import Literal
import uuid

from pydantic import BaseModel, ConfigDict, Field


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class AgentLimits(StrictInput):
    max_concurrency: int = Field(default=1, ge=1, le=4, strict=True)
    bandwidth_bytes_per_second: int = Field(
        default=8 * 1024 * 1024, ge=1024, le=1024**3, strict=True
    )
    max_bytes: int = Field(
        default=64 * 1024 * 1024, ge=1, le=64 * 1024 * 1024, strict=True
    )


class SiteCreate(StrictInput):
    name: str = Field(min_length=1, max_length=100)
    region: str | None = Field(default=None, min_length=1, max_length=100)


class SiteOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    region: str | None
    created_by: uuid.UUID
    created_at: datetime


class SiteList(BaseModel):
    items: list[SiteOut]
    total: int


class AgentCreate(StrictInput):
    name: str = Field(min_length=1, max_length=100)
    limits: AgentLimits = Field(default_factory=AgentLimits)


class AgentRoot(StrictInput):
    root_handle: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    root_revision: int = Field(ge=1, strict=True)


class AgentOut(BaseModel):
    id: uuid.UUID
    site_id: uuid.UUID
    name: str
    protocol_version: int
    credential_revision: int
    state: Literal["pending", "online", "degraded", "offline", "revoked"]
    draining: bool
    revoked_at: datetime | None
    pair_expires_at: datetime | None
    session_id: uuid.UUID | None
    last_seen: datetime | None
    roots: list[AgentRoot]
    limits: AgentLimits
    reported_limits: AgentLimits | None
    created_by: uuid.UUID
    created_at: datetime


class AgentList(BaseModel):
    items: list[AgentOut]
    total: int


class AgentPairingOut(BaseModel):
    agent_id: uuid.UUID
    pair_code: str
    expires_at: datetime


class AgentPairRequest(StrictInput):
    agent_id: uuid.UUID
    pair_code: str = Field(pattern=r"^[A-Za-z0-9_-]{32,256}$")
    protocol_version: int = Field(strict=True)


class AgentCredentialsOut(BaseModel):
    agent_id: uuid.UUID
    credential_revision: int
    token: str
    expires_at: int


class AgentRevisionRequest(StrictInput):
    expected_revision: int | None = Field(default=None, ge=1, strict=True)


class AgentDrainRequest(StrictInput):
    expected_revision: int = Field(ge=1, strict=True)
    draining: bool = Field(strict=True)
