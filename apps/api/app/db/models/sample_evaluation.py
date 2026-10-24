"""Frozen evaluation identity, fenced attempts and reusable sample evidence."""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models.training_dataset import TRAINING_STORAGE_REF_CHECK

_STORAGE_REF_CHECK = TRAINING_STORAGE_REF_CHECK.replace(
    "'training-dataset'", "'sample-evaluation'"
).replace("/datasets/", "/sample-evaluations/")

STATUSES = "'queued','running','cancelling','lost','succeeded','failed','cancelled'"


class SampleEvaluationRun(Base):
    __tablename__ = "sample_evaluation_runs"
    __table_args__ = (
        CheckConstraint(_STORAGE_REF_CHECK, name="ck_sample_evaluation_storage_ref"),
        CheckConstraint(
            "storage_ref IS NULL OR storage_ref->>'prefix' = "
            "'projects/' || project_id::text || '/sample-evaluations/' || id::text || '/'",
            name="ck_sample_evaluation_storage_prefix",
        ),
        UniqueConstraint(
            "project_id", "request_id", name="uq_sample_evaluation_request"
        ),
        Index("ix_sample_evaluation_project_created", "project_id", "created_at"),
        CheckConstraint(f"status IN ({STATUSES})", name="ck_sample_evaluation_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="RESTRICT"),
        nullable=False,
    )
    creator_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    dataset_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("training_dataset_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    model_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("trained_model_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    deployment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("trained_model_deployments.id", ondelete="RESTRICT"),
        nullable=False,
    )
    request_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="queued", nullable=False)
    spec: Mapped[dict] = mapped_column(JSONB, nullable=False)
    spec_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_ref: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    authorization: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    generation: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    current_attempt_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    async_job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("async_jobs.id", ondelete="SET NULL")
    )
    error_code: Mapped[str | None] = mapped_column(String(80))
    error_message: Mapped[str | None] = mapped_column(String(1000))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deletion_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )


class SampleEvaluationAttempt(Base):
    __tablename__ = "sample_evaluation_attempts"
    __table_args__ = (
        UniqueConstraint(
            "run_id", "ordinal", name="uq_sample_evaluation_attempt_ordinal"
        ),
        CheckConstraint(
            "ordinal > 0 AND generation > 0",
            name="ck_sample_evaluation_attempt_generation",
        ),
        CheckConstraint(
            f"status IN ({STATUSES})", name="ck_sample_evaluation_attempt_status"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sample_evaluation_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="queued", nullable=False)
    claim_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    async_job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("async_jobs.id", ondelete="SET NULL")
    )
    report: Mapped[dict | None] = mapped_column(JSONB)
    error_code: Mapped[str | None] = mapped_column(String(80))
    error_message: Mapped[str | None] = mapped_column(String(1000))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    maintenance_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class SampleEvaluationSample(Base):
    __tablename__ = "sample_evaluation_samples"
    __table_args__ = (
        CheckConstraint(
            "ordinal >= 0 AND ordinal < 32", name="ck_sample_evaluation_sample_ordinal"
        ),
        CheckConstraint(
            "status IN ('pending','inflight','succeeded','failed')",
            name="ck_sample_evaluation_sample_status",
        ),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sample_evaluation_runs.id", ondelete="CASCADE"),
        primary_key=True,
    )
    ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), unique=True, nullable=False
    )
    status: Mapped[str] = mapped_column(String(24), default="pending", nullable=False)
    request: Mapped[dict | None] = mapped_column(JSONB)
    evidence: Mapped[dict | None] = mapped_column(JSONB)
    error_code: Mapped[str | None] = mapped_column(String(80))
    error_message: Mapped[str | None] = mapped_column(String(1000))
    produced_attempt_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
