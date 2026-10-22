"""Durable inventory contracts, independent of short-lived job history."""

from datetime import datetime
import uuid

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
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
