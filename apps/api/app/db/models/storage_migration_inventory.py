"""Private bounded source reports follow their owning AsyncJob retention."""

from datetime import datetime
import uuid

from sqlalchemy import (
    Boolean,
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

REPORT_LIMIT = 32 * 1024**2

CHECKS = (
    (
        "status IN ('pending','running','completed','failed','cancelled')",
        "ck_migration_inventory_status",
    ),
    (
        "input_fingerprint ~ '^[0-9a-f]{64}$' AND maintenance_revision >= 1",
        "ck_migration_inventory_input",
    ),
    (
        "error_code IS NULL OR error_code ~ '^[a-z][a-z0-9_]{0,79}$'",
        "ck_migration_inventory_error",
    ),
    (
        "(status='pending' AND started_at IS NULL AND completed_at IS NULL AND operation_id IS NULL) OR (status='running' AND started_at IS NOT NULL AND completed_at IS NULL) OR (status IN ('completed','failed','cancelled') AND completed_at IS NOT NULL)",
        "ck_migration_inventory_lifecycle",
    ),
    (
        "(report IS NULL AND report_sha256 IS NULL AND report_size_bytes IS NULL AND complete IS NULL AND counts IS NULL) OR (report IS NOT NULL AND status IN ('completed','failed') AND complete IS NOT NULL AND counts IS NOT NULL AND report_sha256 ~ '^[0-9a-f]{64}$' AND report_size_bytes=octet_length(report) AND report_size_bytes BETWEEN 1 AND 33554432)",
        "ck_migration_inventory_report",
    ),
    (
        "(status='completed' AND complete IS TRUE AND error_code IS NULL) OR (status='failed' AND error_code IS NOT NULL) OR (status='cancelled' AND cancel_requested_at IS NOT NULL AND report IS NULL) OR status IN ('pending','running')",
        "ck_migration_inventory_outcome",
    ),
)


class StorageMigrationInventory(Base):
    __tablename__ = "storage_migration_inventories"
    __table_args__ = (
        *(CheckConstraint(sql, name=name) for sql, name in CHECKS),
        CheckConstraint(
            "(window_id IS NULL AND window_revision IS NULL) OR (window_id IS NOT NULL AND window_revision IS NOT NULL AND window_revision >= 1)",
            name="ck_migration_inventory_window",
        ),
        Index("ix_migration_inventory_history", "created_at", "async_job_id"),
        Index(
            "uq_migration_inventory_active_source",
            "source_location_id",
            unique=True,
            postgresql_where=text("status IN ('pending','running')"),
        ),
    )

    async_job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("async_jobs.id", ondelete="CASCADE"), primary_key=True
    )
    requested_by: Mapped[uuid.UUID] = mapped_column(nullable=False)
    window_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(
            "storage_migration_windows.id",
            ondelete="RESTRICT",
            name="fk_migration_inventory_window",
        )
    )
    window_revision: Mapped[int | None]
    task_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    source_location_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    source: Mapped[dict] = mapped_column(JSONB, nullable=False)
    limits: Mapped[dict] = mapped_column(JSONB, nullable=False)
    handling: Mapped[dict] = mapped_column(JSONB, nullable=False)
    authority: Mapped[dict] = mapped_column(JSONB, nullable=False)
    input_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    maintenance_revision: Mapped[int] = mapped_column(nullable=False)
    # The permit outlives an interrupted task until actual native close is proven.
    operation_id: Mapped[uuid.UUID | None] = mapped_column(unique=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="pending"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    error_code: Mapped[str | None] = mapped_column(String(80))
    complete: Mapped[bool | None] = mapped_column(Boolean)
    counts: Mapped[dict | None] = mapped_column(JSONB)
    report: Mapped[bytes | None] = mapped_column(LargeBinary, deferred=True)
    report_sha256: Mapped[str | None] = mapped_column(String(64))
    report_size_bytes: Mapped[int | None] = mapped_column()
