"""Successful usage observations survive inventory attempts and job retention."""

from datetime import datetime
import uuid

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class StorageUsageSnapshot(Base):
    __tablename__ = "storage_usage_snapshots"

    scope_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    object_count: Mapped[int | None] = mapped_column(BigInteger)
    total_size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    category_totals: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    object_generation: Mapped[uuid.UUID | None] = mapped_column(UUID)
    measurement_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    measured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt_status: Mapped[str] = mapped_column(String(20), default="never")
    attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(80))


class StorageUsageInventory(Base):
    __tablename__ = "storage_usage_inventories"

    id: Mapped[str] = mapped_column(String(20), primary_key=True)
    async_job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID, ForeignKey("async_jobs.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(String(20))
    authority: Mapped[dict] = mapped_column(JSONB)
    scope: Mapped[list] = mapped_column(JSONB)
    bucket_index: Mapped[int] = mapped_column(default=0)
    checkpoint: Mapped[dict] = mapped_column(JSONB, default=dict)
    success_count: Mapped[int] = mapped_column(default=0)
    failed_count: Mapped[int] = mapped_column(default=0)
    generation: Mapped[int] = mapped_column(default=0)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class StorageUsageMutation(Base):
    """Facts survive caller rollback and never grant object access or deletion."""

    __tablename__ = "storage_usage_mutations"
    __table_args__ = (
        CheckConstraint("kind IN ('create','delete')", name="ck_usage_mutation_kind"),
        CheckConstraint(
            "state IN ('pending','confirmed','unknown')", name="ck_usage_mutation_state"
        ),
        CheckConstraint(
            "physical_outcome IN ('unknown','written','deleted','not_written')",
            name="ck_usage_mutation_physical",
        ),
        CheckConstraint(
            "physical_size_bytes IS NULL OR physical_size_bytes >= 0",
            name="ck_usage_mutation_size",
        ),
        CheckConstraint(
            "current_delta_bytes IS NULL OR (state='confirmed' AND finished_at IS NOT NULL)",
            name="ck_usage_mutation_delta",
        ),
        Index(
            "ix_usage_mutation_address_window", "scope_key", "key_hash", "started_at"
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True)
    scope_key: Mapped[str] = mapped_column(String(64), index=True)
    object_key: Mapped[str] = mapped_column(Text)
    key_hash: Mapped[str] = mapped_column(String(64))
    producer: Mapped[str] = mapped_column(String(32))
    purpose: Mapped[str] = mapped_column(String(32))
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID)
    kind: Mapped[str] = mapped_column(String(12))
    expected_version: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str] = mapped_column(
        String(12), default="pending", server_default="pending"
    )
    physical_outcome: Mapped[str] = mapped_column(
        String(16), default="unknown", server_default="unknown"
    )
    physical_size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    current_delta_bytes: Mapped[int | None] = mapped_column(BigInteger)
    evidence: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class StorageUsageInventoryObject(Base):
    __tablename__ = "storage_usage_inventory_objects"
    __table_args__ = (
        CheckConstraint("size_bytes >= 0", name="ck_usage_inventory_object_size"),
        CheckConstraint(
            "observed_through >= observed_from", name="ck_usage_inventory_object_window"
        ),
    )
    generation: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True)
    scope_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    key_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    object_key: Mapped[str] = mapped_column(Text)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    observed_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    observed_through: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class StorageUsageReconciliation(Base):
    __tablename__ = "storage_usage_reconciliations"
    generation: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True)
    scope_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    previous_generation: Mapped[uuid.UUID | None] = mapped_column(UUID)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    measured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    summary: Mapped[dict] = mapped_column(JSONB)


class StorageUsageSchedule(Base):
    __tablename__ = "storage_usage_schedules"
    __table_args__ = (
        CheckConstraint(
            "interval_hours BETWEEN 1 AND 168", name="ck_usage_schedule_interval"
        ),
        CheckConstraint(
            "status IN ('off','active','paused')", name="ck_usage_schedule_status"
        ),
    )
    id: Mapped[str] = mapped_column(String(20), primary_key=True)
    enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    interval_hours: Mapped[int] = mapped_column(
        Integer, default=24, server_default="24"
    )
    status: Mapped[str] = mapped_column(String(12), default="off", server_default="off")
    authority: Mapped[dict] = mapped_column(JSONB)
    scope: Mapped[list] = mapped_column(JSONB)
    next_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pause_reason: Mapped[str | None] = mapped_column(String(80))
