from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Index,
    PrimaryKeyConstraint,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AssetStorageDeletion(Base):
    """Durable prefix cleanup work owned by Assets and retried out of band."""

    __tablename__ = "asset_storage_deletions"
    __table_args__ = (
        PrimaryKeyConstraint(
            "operation_id", "asset_id", name="pk_asset_storage_deletions"
        ),
        CheckConstraint(
            "char_length(bucket) BETWEEN 3 AND 63 "
            "AND bucket ~ '^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$' "
            "AND bucket !~ '^[0-9]+(\\.[0-9]+){3}$'",
            name="ck_asset_storage_deletions_bucket",
        ),
        CheckConstraint(
            "prefix ~ '^image-pyramids/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/$'",
            name="ck_asset_storage_deletions_prefix",
        ),
        Index(
            "ix_asset_storage_deletions_pending_created",
            "created_at",
            "operation_id",
            "asset_id",
            postgresql_where=text("completed_at IS NULL"),
        ),
    )

    operation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True
    )
    asset_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    bucket: Mapped[str] = mapped_column(String(63), nullable=False)
    prefix: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
