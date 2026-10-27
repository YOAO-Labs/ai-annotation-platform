"""v0.11.16 · 存储连接器 schema。

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
    # 非密钥配置：见 Assets source 的 management validator 校验必填项。
    #   s3   → {endpoint, bucket, region?, base_prefix?, use_ssl?}
    #   sftp → {host, port?, username, base_path?, auth_type("password"|"key")}
    config: dict
    # 明文密钥（写入即加密，绝不回吐）：
    #   s3   → {access_key, secret_key}
    #   sftp → {password} 或 {private_key, passphrase?}
    secret: dict
    # scope=owner 默认归属创建者；global 仅超管可建。project_id 为历史兼容字段，不再写入。
    scope: StorageScope = "owner"
    project_id: uuid.UUID | None = None


class StorageConnectionUpdate(BaseModel):
    expected_revision: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=255)
    config: dict | None = None
    # 留空 = 不改密钥；给出则整体轮换。
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
