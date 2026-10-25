"""Durable experiments and fenced execution on exclusive GPU resources."""

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
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models.training_dataset import TRAINING_STORAGE_REF_CHECK

TRAINING_RUN_STORAGE_REF_CHECK = TRAINING_STORAGE_REF_CHECK.replace(
    "'training-dataset'", "'training-run'"
).replace("/datasets/", "/runs/")


class TrainingRun(Base):
    __tablename__ = "training_runs"
    __table_args__ = (
        UniqueConstraint("project_id", "request_id", name="uq_training_run_request"),
        UniqueConstraint("id", "study_id", name="uq_training_run_study_identity"),
        Index(
            "uq_training_study_active_run",
            "study_id",
            unique=True,
            postgresql_where=text(
                "study_id IS NOT NULL AND status IN ('queued','preparing','training','evaluating','uploading','cancelling','lost')"
            ),
        ),
        Index("ix_training_run_project_created", "project_id", "created_at"),
        Index(
            "ix_lineage_training_runs_initialization_model_id",
            "initialization_model_id",
            "created_at",
            "id",
        ),
        Index(
            "uq_training_auto_active_project",
            "project_id",
            unique=True,
            postgresql_where=text(
                "auto_trigger_id IS NOT NULL AND status IN ('queued','preparing','training','evaluating','uploading','cancelling','lost')"
            ),
        ),
        CheckConstraint(
            "status IN ('queued','preparing','training','evaluating','uploading','cancelling','lost','succeeded','failed','cancelled')",
            name="ck_training_run_status",
        ),
        CheckConstraint(
            TRAINING_RUN_STORAGE_REF_CHECK, name="ck_training_run_storage_ref"
        ),
        CheckConstraint(
            "storage_ref IS NULL OR storage_ref->>'prefix' = COALESCE(storage_ref->>'key_prefix','') || "
            "'projects/' || project_id::text || '/runs/' || id::text || '/'",
            name="ck_training_run_storage_prefix",
        ),
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
    study_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("training_studies.id", ondelete="RESTRICT")
    )
    dataset_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("training_dataset_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    initialization_model_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "trained_model_versions.id",
            name="fk_training_run_initialization_model",
            ondelete="RESTRICT",
            use_alter=True,
        ),
    )
    auto_trigger_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("learning_triggers.id", ondelete="RESTRICT"),
        unique=True,
    )
    async_job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("async_jobs.id", ondelete="SET NULL")
    )
    request_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="queued", nullable=False)
    config: Mapped[dict] = mapped_column(JSONB, nullable=False)
    config_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    origin: Mapped[dict | None] = mapped_column(JSONB)
    storage_ref: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    authorization: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    current_attempt_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    metrics: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    error_message: Mapped[str | None] = mapped_column(String(1000))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TrainingAttempt(Base):
    __tablename__ = "training_attempts"
    __table_args__ = (
        UniqueConstraint("run_id", "ordinal", name="uq_training_attempt_ordinal"),
        CheckConstraint("ordinal > 0", name="ck_training_attempt_ordinal"),
        CheckConstraint(
            "status IN ('queued','preparing','training','evaluating','uploading','cancelling','lost','succeeded','failed','cancelled')",
            name="ck_training_attempt_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("training_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="queued", nullable=False)
    resume_from_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("training_attempts.id", ondelete="RESTRICT")
    )
    executor_id: Mapped[str | None] = mapped_column(String(100))
    boot_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    lease_digest: Mapped[str | None] = mapped_column(String(64))
    claim_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    finish_request_digest: Mapped[str | None] = mapped_column(String(64))
    resource_id: Mapped[str | None] = mapped_column(String(200))
    runtime: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    upload_inventory: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    artifacts: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    observation_state: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    failure: Mapped[dict | None] = mapped_column(JSONB)
    native_budget: Mapped[dict | None] = mapped_column(JSONB)
    native_accounting: Mapped[dict | None] = mapped_column(JSONB)
    error_message: Mapped[str | None] = mapped_column(String(1000))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TrainingResourceLease(Base):
    __tablename__ = "training_resource_leases"

    resource_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    attempt_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("training_attempts.id", ondelete="RESTRICT"),
        unique=True,
    )
    executor_id: Mapped[str | None] = mapped_column(String(100))
    boot_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    runtime: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class TrainingEvent(Base):
    __tablename__ = "training_events"

    attempt_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("training_attempts.id", ondelete="CASCADE"),
        primary_key=True,
    )
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
