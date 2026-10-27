"""Operator-bound final snapshots outlive retained jobs and native CLI processes."""

from datetime import datetime
import uuid

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

STATES = ("bound", "sealing", "sealed", "invalidating", "invalidated")


class StorageMigrationFinalization(Base):
    __tablename__ = "storage_migration_finalizations"
    __table_args__ = (
        CheckConstraint(
            "state IN ('bound','sealing','sealed','invalidating','invalidated')",
            name="ck_migration_finalization_state",
        ),
        CheckConstraint(
            "revision >= 1 AND window_revision >= 1 AND source_gate_revision >= 1 AND source_config_revision >= 1",
            name="ck_migration_finalization_revision",
        ),
        CheckConstraint(
            "error_code IS NULL OR error_code ~ '^[a-z][a-z0-9_]{0,79}$'",
            name="ck_migration_finalization_error",
        ),
        CheckConstraint(
            "octet_length(claim::text) <= 4096 AND octet_length(native::text) <= 65536 "
            "AND (token IS NULL OR octet_length(token::text) <= 65536) "
            "AND (evidence IS NULL OR octet_length(evidence::text) <= 65536)",
            name="ck_migration_finalization_budget",
        ),
        CheckConstraint(
            "state <> 'sealed' OR (copy_id IS NOT NULL AND token IS NOT NULL AND evidence IS NOT NULL AND sealed_at IS NOT NULL)",
            name="ck_migration_finalization_sealed",
        ),
        CheckConstraint(
            "(state = 'invalidated') = (invalidated_at IS NOT NULL)",
            name="ck_migration_finalization_invalidated",
        ),
        Index(
            "uq_migration_finalization_active_window",
            "window_id",
            unique=True,
            postgresql_where=text("state <> 'invalidated'"),
        ),
        Index("ix_migration_finalization_copy", "copy_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    window_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("storage_migration_windows.id", ondelete="RESTRICT")
    )
    state: Mapped[str] = mapped_column(String(20), default="bound")
    revision: Mapped[int] = mapped_column(default=1)
    window_revision: Mapped[int]
    source_location_id: Mapped[uuid.UUID]
    source: Mapped[dict] = mapped_column(JSONB)
    source_gate_revision: Mapped[int]
    source_config_revision: Mapped[int]
    parent_copy_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("storage_migration_copies.id", ondelete="RESTRICT")
    )
    target_location_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("storage_locations.id", ondelete="RESTRICT")
    )
    target: Mapped[dict] = mapped_column(JSONB)
    claim: Mapped[dict] = mapped_column(JSONB)
    native: Mapped[dict] = mapped_column(JSONB)
    copy_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("storage_migration_copies.id", ondelete="RESTRICT")
    )
    # Historical UUID, deliberately independent of AsyncJob/report retention.
    inventory_job_id: Mapped[uuid.UUID | None]
    token: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    evidence: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    error_code: Mapped[str | None] = mapped_column(String(80))
    created_by: Mapped[uuid.UUID]
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    sealed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
