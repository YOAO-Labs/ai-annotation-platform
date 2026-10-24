"""Metadata-only identities for authenticated original downloads."""

from typing import Literal
from uuid import UUID
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class OriginalIdentity(BaseModel):
    owner_kind: Literal["dataset_asset", "task_original"]
    content_id: UUID
    sha256: str
    size_bytes: int
    media_type: str
    verification_state: Literal["verified"]
    original_availability: Literal["unchecked"]
    download_path: str


class OriginalPlaybackPrepare(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content_id: UUID
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    session_id: UUID


class OriginalPlaybackRelease(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: UUID


class OriginalPlaybackLease(BaseModel):
    lease_id: UUID
    media_path: str
    expires_at: datetime
