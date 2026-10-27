"""v0.11.16 · 存储连接器（外部 S3 / SFTP）。

服务端主动拉取类导入的可复用连接配置。非密钥部分（endpoint/host/bucket/path 等）
存 ``config`` JSONB；密钥（AK/SK、SSH 密码/私钥）经 Fernet 加密存 ``secret_enc``，
绝不回吐明文。``scope`` 区分全局（超管）与个人归属（创建者可用）。

实际拉取与白名单/SSRF 校验见 app/services/connector_guard.py 与 v0.11.15 的 SourceAdapter。
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class StorageConnectionKind(str, enum.Enum):
    S3 = "s3"  # 外部对象存储（S3 / OSS / 兼容 MinIO）
    SFTP = "sftp"  # SFTP/SSH 目标（宿主机 & 同网段服务器）


class StorageConnectionScope(str, enum.Enum):
    GLOBAL = "global"  # 超管建，全平台可见
    OWNER = "owner"  # 用户自建，仅创建者与超管可见


class StorageConnection(Base):
    __tablename__ = "storage_connections"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    revision: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    # 非密钥配置：s3 → endpoint/region/bucket/base_prefix/use_ssl；
    # sftp → host/port/username/base_path/auth_type。
    config: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # Fernet 密文：s3 → {access_key,secret_key}；sftp → {password} 或 {private_key,passphrase?}
    secret_enc: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    scope: Mapped[str] = mapped_column(
        String(20), nullable=False, default=StorageConnectionScope.OWNER.value
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=True,
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        Index("ix_storage_connections_scope_project", "scope", "project_id"),
    )
