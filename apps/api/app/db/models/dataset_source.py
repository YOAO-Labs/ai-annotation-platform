"""Durable inventory contracts, independent of short-lived job history."""

from datetime import datetime
import uuid

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class DatasetSource(Base):
    __tablename__ = "dataset_sources"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    dataset_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("datasets.id", ondelete="SET NULL"), index=True
    )
    location_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("storage_locations.id", ondelete="RESTRICT"), index=True
    )
    name: Mapped[str] = mapped_column(String(100))
    config: Mapped[dict] = mapped_column(JSONB)
    config_revision: Mapped[int] = mapped_column(default=1, server_default="1")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    generation: Mapped[int] = mapped_column(default=0, server_default="0")
    root_identity: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class DatasetScanRun(Base):
    __tablename__ = "dataset_scan_runs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("dataset_sources.id", ondelete="RESTRICT"), index=True
    )
    async_job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("async_jobs.id", ondelete="SET NULL"), unique=True
    )
    snapshot: Mapped[dict] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(
        String(20), default="pending", server_default="pending"
    )
    phase: Mapped[str] = mapped_column(
        String(30), default="enumerating", server_default="enumerating"
    )
    complete: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    generation: Mapped[int] = mapped_column(default=0, server_default="0")
    lease_token: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    error_code: Mapped[str | None] = mapped_column(String(60), nullable=True)
    counts: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','running','completed','failed','cancelled')",
            name="ck_dataset_scan_status",
        ),
        Index(
            "uq_dataset_source_active_scan",
            "source_id",
            unique=True,
            postgresql_where=text("status IN ('pending','running')"),
        ),
    )


class DatasetScanEntry(Base):
    __tablename__ = "dataset_scan_entries"

    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("dataset_scan_runs.id", ondelete="CASCADE"), primary_key=True
    )
    path_digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    relative_path: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(20))
    state: Mapped[str] = mapped_column(String(20))
    observed: Mapped[dict] = mapped_column(JSONB)
    verified_content: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    directory_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    validated_generation: Mapped[int] = mapped_column(default=0, server_default="0")

    __table_args__ = (
        Index(
            "ix_dataset_scan_work", "run_id", "kind", "state", "validated_generation"
        ),
    )


class DatasetReference(Base):
    """A business item permanently names one observed, verified content revision."""

    __tablename__ = "dataset_references"

    dataset_item_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("dataset_items.id", ondelete="CASCADE"), primary_key=True
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("dataset_sources.id", ondelete="RESTRICT"), index=True
    )
    run_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    path_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    source_revision: Mapped[int] = mapped_column(nullable=False)
    location_revision: Mapped[int] = mapped_column(nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(
            ["run_id", "path_digest"],
            ["dataset_scan_entries.run_id", "dataset_scan_entries.path_digest"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("source_id", "path_digest", name="uq_reference_source_path"),
    )


class DatasetRegistrationRun(Base):
    """Durable preflight/registration progress, independent of job retention."""

    __tablename__ = "dataset_registration_runs"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    scan_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("dataset_scan_runs.id", ondelete="RESTRICT"), unique=True
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("dataset_sources.id", ondelete="RESTRICT"), index=True
    )
    async_job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("async_jobs.id", ondelete="SET NULL"), unique=True
    )
    snapshot: Mapped[dict] = mapped_column(JSONB)
    phase: Mapped[str] = mapped_column(String(20), default="preflight")
    status: Mapped[str] = mapped_column(String(20), default="pending")
    generation: Mapped[int] = mapped_column(default=0)
    lease_token: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(60))
    counts: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','running','completed','failed','cancelled')",
            name="ck_dataset_registration_status",
        ),
        CheckConstraint(
            "phase IN ('preflight','registering')", name="ck_dataset_registration_phase"
        ),
        Index(
            "uq_dataset_source_active_registration",
            "source_id",
            unique=True,
            postgresql_where=text("status IN ('pending','running')"),
        ),
    )


class DatasetRegistrationEntry(Base):
    __tablename__ = "dataset_registration_entries"
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("dataset_registration_runs.id", ondelete="CASCADE"), primary_key=True
    )
    path_digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    state: Mapped[str] = mapped_column(String(20), default="pending")
    item_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("dataset_items.id", ondelete="SET NULL")
    )
    error_code: Mapped[str | None] = mapped_column(String(60))
    attempts: Mapped[int] = mapped_column(default=0)
    refresh_unverified: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (
        CheckConstraint(
            "state IN ('pending','ready','registered','unchanged','error')",
            name="ck_dataset_registration_entry_state",
        ),
        Index("ix_dataset_registration_work", "run_id", "state", "path_digest"),
    )
