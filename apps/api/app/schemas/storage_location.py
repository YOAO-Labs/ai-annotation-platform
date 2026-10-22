"""Public registry contracts; deployment paths and node anchors stay private."""

from datetime import datetime
from typing import Literal
import uuid

from pydantic import BaseModel, ConfigDict, Field


class FilesystemLocationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    root_handle: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    relative_path: str = Field(default="", max_length=1000)
    scope: Literal["restricted", "global"] = "restricted"
    use_user_ids: list[uuid.UUID] = Field(default_factory=list, max_length=100)


class StorageLocationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=100)
    enabled: bool | None = None
    scope: Literal["restricted", "global"] | None = None
    use_user_ids: list[uuid.UUID] | None = Field(default=None, max_length=100)


class StorageLocationOut(BaseModel):
    id: uuid.UUID
    name: str
    kind: str
    enabled: bool
    config_revision: int
    connection_id: uuid.UUID | None = None
    root_handle: str | None = None
    relative_path: str | None = None
    scope: str | None = None
    use_user_ids: list[uuid.UUID] = Field(default_factory=list)
    declared_capabilities: dict[str, bool]


class StorageLocationList(BaseModel):
    items: list[StorageLocationOut]
    total: int


class DirectoryEntry(BaseModel):
    name: str
    relative_path: str


class StorageDirectoryPage(BaseModel):
    entries: list[DirectoryEntry]
    next_cursor: str | None


class StorageProbeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node_id: str = Field(
        default="api", min_length=1, max_length=248, pattern=r"^[a-zA-Z0-9_.@:-]+$"
    )
    sample_file: str | None = Field(default=None, max_length=1000)


class StorageProbeOut(BaseModel):
    node_id: str
    config_revision: int
    observed_at: datetime
    expires_at: datetime
    ok: bool
    code: str
    capabilities: dict[str, str]


class StorageBindingOut(BaseModel):
    purpose: str
    location_id: uuid.UUID | None = None
    generation: int | None = None
    bucket: str | None = None
    state: Literal["missing", "configured", "mismatch"]
