"""Durable exact-version deletion responsibility and permanent publication fences."""

from datetime import datetime
import uuid

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
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


class ManagedOriginalReclaimAttempt(Base):
    __tablename__ = "managed_original_reclaim_attempts"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','running','completed','failed','cancelled','unknown')",
            name="ck_reclaim_status",
        ),
        CheckConstraint(
            "phase IN ('claimed','reading','delete_intent','delete_ack','absence_verified','settled')",
            name="ck_reclaim_phase",
        ),
        CheckConstraint(
            "outcome IS NULL OR outcome IN ('deleted','already_absent','missing','changed','failed','unknown','cancelled')",
            name="ck_reclaim_outcome",
        ),
        CheckConstraint(
            "expected_size_bytes >= 0 AND expected_sha256 ~ '^[0-9a-f]{64}$' AND input_fingerprint ~ '^[0-9a-f]{64}$' AND maintenance_revision >= 1",
            name="ck_reclaim_identity",
        ),
        CheckConstraint(
            "length(version_id)>0 AND version_id<>'null'", name="ck_reclaim_version"
        ),
        CheckConstraint(
            "error_code IS NULL OR error_code ~ '^[a-z][a-z0-9_]{0,79}$'",
            name="ck_reclaim_error",
        ),
        CheckConstraint(
            "(status IN ('pending','running') AND completed_at IS NULL AND outcome IS NULL) OR (status NOT IN ('pending','running') AND completed_at IS NOT NULL AND outcome IS NOT NULL)",
            name="ck_reclaim_terminal",
        ),
        CheckConstraint(
            "delete_ack IS NULL OR delete_intent_at IS NOT NULL",
            name="ck_reclaim_ack_intent",
        ),
        CheckConstraint(
            "absence_verified_at IS NULL OR delete_intent_at IS NOT NULL OR retry_of_id IS NOT NULL",
            name="ck_reclaim_absence",
        ),
        CheckConstraint(
            "status<>'completed' OR (outcome IN ('deleted','already_absent') AND absence_verified_at IS NOT NULL AND physical_settled)",
            name="ck_reclaim_success",
        ),
        Index("ix_reclaim_history", "retirement_id", "requested_at", "id"),
        Index(
            "uq_reclaim_active_creation",
            "creation_id",
            unique=True,
            postgresql_where=text("status IN ('pending','running')"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    retirement_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("managed_original_retirements.id", ondelete="RESTRICT"),
        nullable=False,
    )
    creation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("managed_original_creations.id", ondelete="RESTRICT"), nullable=False
    )
    async_job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("async_jobs.id", ondelete="SET NULL"), unique=True
    )
    retry_of_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("managed_original_reclaim_attempts.id", ondelete="RESTRICT")
    )
    requested_by: Mapped[uuid.UUID] = mapped_column(nullable=False)
    task_id: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    authority: Mapped[dict] = mapped_column(JSONB, nullable=False)
    target: Mapped[dict] = mapped_column(JSONB, nullable=False)
    input_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    version_id: Mapped[str] = mapped_column(Text, nullable=False)
    expected_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    expected_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    maintenance_revision: Mapped[int] = mapped_column(nullable=False)
    reason: Mapped[str] = mapped_column(String(1000), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="pending"
    )
    phase: Mapped[str] = mapped_column(
        String(24), nullable=False, server_default="claimed"
    )
    outcome: Mapped[str | None] = mapped_column(String(24))
    error_code: Mapped[str | None] = mapped_column(String(80))
    operation_id: Mapped[uuid.UUID | None] = mapped_column(unique=True)
    physical_settled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    read_verified: Mapped[dict | None] = mapped_column(JSONB)
    delete_ack: Mapped[dict | None] = mapped_column(JSONB)
    observation: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delete_intent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    absence_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )


class ManagedOriginalReclaimClaim(Base):
    __tablename__ = "managed_original_reclaim_claims"
    __table_args__ = (
        UniqueConstraint("bucket", "object_key", name="uq_reclaim_physical_group"),
        CheckConstraint(
            "state IN ('released','claimed','tombstone','deleted') AND revision>=1",
            name="ck_reclaim_claim_state",
        ),
        CheckConstraint(
            "length(version_id)>0 AND version_id<>'null' AND storage_identity ~ '^[0-9a-f]{64}$'",
            name="ck_reclaim_claim_identity",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    bucket: Mapped[str] = mapped_column(String(255), nullable=False)
    object_key: Mapped[str] = mapped_column(Text, nullable=False)
    storage_identity: Mapped[str] = mapped_column(String(64), nullable=False)
    creation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("managed_original_creations.id", ondelete="RESTRICT"), nullable=False
    )
    version_id: Mapped[str] = mapped_column(Text, nullable=False)
    current_attempt_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("managed_original_reclaim_attempts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    state: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="claimed"
    )
    revision: Mapped[int] = mapped_column(nullable=False, server_default="1")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class ReferenceReconciliationState(Base):
    __tablename__ = "managed_original_reference_reconciliation"
    __table_args__ = (
        CheckConstraint(
            "id=1 AND status IN ('idle','pending','running','completed','failed')",
            name="ck_reference_reconciliation_singleton",
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="idle"
    )
    async_job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("async_jobs.id", ondelete="SET NULL")
    )
    task_id: Mapped[str | None] = mapped_column(String(255))
    measured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    counts: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    unknown: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    deltas: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    error_code: Mapped[str | None] = mapped_column(String(80))
