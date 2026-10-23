"""Recovery and quota ownership survive deletion of business assets and sources."""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class OriginalCacheEntry(Base):
    __tablename__ = "original_cache_entries"
    __table_args__ = (
        CheckConstraint(
            "state IN ('uploading','ready','deleting')", name="ck_original_cache_state"
        ),
        CheckConstraint("size_bytes > 0", name="ck_original_cache_size"),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_original_cache_sha"),
        Index("ix_original_cache_due", "next_check_at", "id"),
        Index(
            "ix_original_cache_quota", "scope_location_id", "state", "last_accessed_at"
        ),
        Index(
            "uq_original_cache_current",
            "asset_id",
            unique=True,
            postgresql_where=text("state IN ('uploading','ready')"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    # No business FKs: a cascade must never erase object deletion/accounting.
    scope_location_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    source_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    asset_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_revision: Mapped[int] = mapped_column(nullable=False)
    policy_revision: Mapped[int] = mapped_column(nullable=False)
    location_revision: Mapped[int] = mapped_column(nullable=False)
    location_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    bucket: Mapped[str] = mapped_column(String(255), nullable=False)
    binding_generation: Mapped[int] = mapped_column(nullable=False)
    storage_identity: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    uncertain_write: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_accessed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    writer_until: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    retain_until: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    next_check_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    error_code: Mapped[str | None] = mapped_column(String(64))


class OriginalCacheReadLease(Base):
    __tablename__ = "original_cache_read_leases"
    __table_args__ = (Index("ix_original_cache_read_lease", "entry_id", "expires_at"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    entry_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("original_cache_entries.id", ondelete="CASCADE")
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
