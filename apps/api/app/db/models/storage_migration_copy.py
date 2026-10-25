"""Durable frozen migration plans, attempts and per-object physical evidence."""

from datetime import datetime
import uuid

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    LargeBinary,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

BASELINE_LIMIT = 256 * 1024**2
CATALOG_LIMIT = 64 * 1024**2
ROW_LIMIT = 100000


class StorageMigrationCopy(Base):
    __tablename__ = "storage_migration_copies"
    __table_args__ = (
        CheckConstraint(
            "octet_length(source_report) BETWEEN 1 AND 33554432",
            name="ck_migration_copy_source_bytes",
        ),
        CheckConstraint(
            "catalog IS NULL OR octet_length(catalog) BETWEEN 1 AND 67108864",
            name="ck_migration_copy_catalog_bytes",
        ),
        CheckConstraint(
            "baseline IS NULL OR octet_length(baseline) BETWEEN 1 AND 268435456",
            name="ck_migration_copy_baseline_bytes",
        ),
        CheckConstraint(
            "delta_bytes BETWEEN 0 AND 268435456 AND delta_sequence >= 0",
            name="ck_migration_copy_delta_budget",
        ),
        Index("ix_migration_copy_history", "created_at", "id"),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    input_fingerprint: Mapped[str] = mapped_column(String(64), unique=True)
    source_inventory_job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("async_jobs.id", ondelete="SET NULL")
    )
    source_report: Mapped[bytes] = mapped_column(LargeBinary, deferred=True)
    source_report_sha256: Mapped[str] = mapped_column(String(64))
    source_complete: Mapped[bool]
    source: Mapped[dict] = mapped_column(JSONB)
    target_location_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("storage_locations.id", ondelete="RESTRICT"), index=True
    )
    target: Mapped[dict] = mapped_column(JSONB)
    handling: Mapped[dict] = mapped_column(JSONB)
    limits: Mapped[dict] = mapped_column(JSONB)
    catalog: Mapped[bytes | None] = mapped_column(LargeBinary, deferred=True)
    catalog_sha256: Mapped[str | None] = mapped_column(String(64))
    baseline: Mapped[bytes | None] = mapped_column(LargeBinary, deferred=True)
    baseline_sha256: Mapped[str | None] = mapped_column(String(64))
    summary: Mapped[dict | None] = mapped_column(JSONB)
    last_attempt_id: Mapped[uuid.UUID | None]
    delta_sequence: Mapped[int] = mapped_column(default=0, server_default="0")
    delta_bytes: Mapped[int] = mapped_column(default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class StorageMigrationCopyAttempt(Base):
    __tablename__ = "storage_migration_copy_attempts"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','running','completed','failed','cancelled')",
            name="ck_migration_copy_attempt_status",
        ),
        CheckConstraint(
            "operation IN ('prepare','run','verify')",
            name="ck_migration_copy_attempt_operation",
        ),
        Index(
            "uq_migration_copy_active_attempt",
            "copy_id",
            unique=True,
            postgresql_where=text("status IN ('pending','running')"),
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    copy_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("storage_migration_copies.id", ondelete="CASCADE"), index=True
    )
    async_job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("async_jobs.id", ondelete="SET NULL"), unique=True
    )
    task_id: Mapped[str] = mapped_column(String(64), unique=True)
    requested_by: Mapped[uuid.UUID]
    authority: Mapped[dict] = mapped_column(JSONB)
    scope: Mapped[dict] = mapped_column(JSONB)
    operation: Mapped[str] = mapped_column(String(12))
    retry_failed: Mapped[bool] = mapped_column(default=False)
    retry_object_ids: Mapped[list | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    source_operation_id: Mapped[uuid.UUID | None] = mapped_column(unique=True)
    target_operation_id: Mapped[uuid.UUID | None] = mapped_column(unique=True)
    process: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    error_code: Mapped[str | None] = mapped_column(String(80))
    result: Mapped[dict | None] = mapped_column(JSONB)


class StorageMigrationCopyDelta(Base):
    __tablename__ = "storage_migration_copy_deltas"
    __table_args__ = (
        CheckConstraint(
            "sequence >= 1 AND size_bytes BETWEEN 1 AND 262144",
            name="ck_migration_copy_delta_row",
        ),
    )
    copy_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("storage_migration_copies.id", ondelete="CASCADE"), primary_key=True
    )
    object_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    sequence: Mapped[int]
    data: Mapped[dict] = mapped_column(JSONB)
    digest: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int]
