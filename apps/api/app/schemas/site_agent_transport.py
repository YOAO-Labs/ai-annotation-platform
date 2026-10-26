"""Private outbound Agent transport envelopes; no operator root paths."""

from typing import Literal
import uuid

from pydantic import BaseModel, ConfigDict, Field, StrictInt


class RootAdvertisement(BaseModel):
    model_config = ConfigDict(extra="forbid")
    root_handle: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    root_revision: StrictInt = Field(ge=1)


class AgentReportedLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_concurrency: StrictInt = Field(ge=1, le=4)
    bandwidth_bytes_per_second: StrictInt = Field(ge=1024, le=1024**3)
    max_bytes: StrictInt = Field(ge=1, le=64 * 1024 * 1024)


class AgentPollRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    protocol_version: StrictInt = Field(ge=1, le=1)
    agent_id: uuid.UUID
    credential_revision: StrictInt = Field(ge=1)
    session_id: uuid.UUID
    roots: list[RootAdvertisement] = Field(max_length=32)
    limits: AgentReportedLimits


class AgentPollResponse(BaseModel):
    protocol_version: Literal[1] = 1
    command: dict | None


class AgentCommandAck(BaseModel):
    protocol_version: Literal[1] = 1
    command_id: uuid.UUID
    command_digest: str
    result_digest: str
    state: Literal["received", "applied", "rejected"]
    code: str | None


class AgentChunkAck(BaseModel):
    command_id: uuid.UUID
    command_digest: str
    offset: int
    sha256: str
