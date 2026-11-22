"""存储连接器 schema。

Out **绝不含密钥字段**，仅以 secret_set:bool 表达"是否已配密钥"。
config 仅含非密钥项（endpoint/host/bucket/username/path），可原样回吐。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field

StorageKind = Literal["s3", "sftp"]
StorageScope = Literal["global", "owner"]


class StorageConnectionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    kind: StorageKind
    config: dict
    secret: dict
    scope: StorageScope = "owner"
    project_id: uuid.UUID | None = None


class StorageConnectionUpdate(BaseModel):
    expected_revision: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=255)
    config: dict | None = None
    secret: dict | None = None


class StorageConnectionOut(BaseModel):
    id: uuid.UUID
    revision: int
    name: str
    kind: StorageKind
    config: dict
    scope: StorageScope
    project_id: uuid.UUID | None
    secret_set: bool
    created_by: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


class StorageConnectionTestResult(BaseModel):
    ok: bool
    message: str
    sample_count: int | None = None


class ConnectorAllowlistOut(BaseModel):
    entries: list[str]
    source: Literal["database", "environment"]


class ConnectorAllowlistUpdate(BaseModel):
    entries: list[Annotated[str, Field(max_length=253)]] = Field(max_length=256)


class ConnectorDeploymentSftpPresetOut(BaseModel):
    enabled: bool
    host: str | None = None
    port: int = 22
