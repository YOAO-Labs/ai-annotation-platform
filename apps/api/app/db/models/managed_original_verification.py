"""Durable observations; physical responsibility survives business and job deletion."""

from datetime import datetime
import uuid

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ManagedOriginalVerification(Base):
    __tablename__ = "managed_original_verifications"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','running','completed','failed','cancelled')",
            name="ck_managed_verification_status",
        ),
        CheckConstraint(
            "expected_sha256 ~ '^[0-9a-f]{64}$' AND expected_size_bytes >= 0 AND maintenance_revision >= 1",
            name="ck_managed_verification_expected",
        ),
        CheckConstraint(
            "input_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_managed_verification_fingerprint",
        ),
        CheckConstraint(
            "observed_sha256 IS NULL OR observed_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_managed_verification_observed_sha",
        ),
        CheckConstraint(
            "observed_size_bytes IS NULL OR observed_size_bytes >= 0",
            name="ck_managed_verification_observed_size",
        ),
        CheckConstraint(
            "error_code IS NULL OR error_code ~ '^[a-z][a-z0-9_]{0,79}$'",
            name="ck_managed_verification_error",
        ),
        CheckConstraint(
            "(status='pending' AND started_at IS NULL AND operation_id IS NULL AND completed_at IS NULL AND outcome IS NULL) OR (status='running' AND started_at IS NOT NULL AND completed_at IS NULL AND outcome IS NULL) OR (status='completed' AND started_at IS NOT NULL AND operation_id IS NOT NULL AND completed_at IS NOT NULL AND outcome IN ('matched','missing','changed','unverifiable')) OR (status='failed' AND completed_at IS NOT NULL AND outcome='failed' AND error_code IS NOT NULL) OR (status='cancelled' AND completed_at IS NOT NULL AND outcome='cancelled' AND cancel_requested_at IS NOT NULL)",
            name="ck_managed_verification_lifecycle",
        ),
        CheckConstraint(
            "(status IN ('pending','running') AND outcome IS NULL AND error_code IS NULL AND observed_sha256 IS NULL AND observed_size_bytes IS NULL AND observation='{}'::jsonb) OR (status NOT IN ('pending','running') AND outcome IS NOT NULL)",
            name="ck_managed_verification_terminal_facts",
        ),
        CheckConstraint(
            "outcome IS DISTINCT FROM 'matched' OR (observed_sha256 IS NOT NULL AND observed_size_bytes IS NOT NULL AND observed_sha256=expected_sha256 AND observed_size_bytes=expected_size_bytes AND error_code IS NULL)",
            name="ck_managed_verification_match",
        ),
        Index("ix_managed_verification_history", "retirement_id", "requested_at", "id"),
        Index(
            "uq_managed_verification_active",
            "retirement_id",
            unique=True,
            postgresql_where=text("status IN ('pending','running')"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    retirement_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("managed_original_retirements.id", ondelete="RESTRICT"),
        nullable=False,
    )
    async_job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("async_jobs.id", ondelete="SET NULL"), unique=True
    )
    requested_by: Mapped[uuid.UUID] = mapped_column(nullable=False)
    task_id: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    authority: Mapped[dict] = mapped_column(JSONB, nullable=False)
    anchor: Mapped[dict] = mapped_column(JSONB, nullable=False)
    configuration: Mapped[dict] = mapped_column(JSONB, nullable=False)
    input_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    expected_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    expected_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    maintenance_revision: Mapped[int] = mapped_column(nullable=False)
    # No FK: StorageOperation is independently deleted only after actual I/O settles.
    operation_id: Mapped[uuid.UUID | None] = mapped_column(unique=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="pending"
    )
    outcome: Mapped[str | None] = mapped_column(String(20))
    error_code: Mapped[str | None] = mapped_column(String(80))
    observed_sha256: Mapped[str | None] = mapped_column(String(64))
    observed_size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    observation: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
