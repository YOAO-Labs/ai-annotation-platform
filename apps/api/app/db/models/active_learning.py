"""Retained selection evidence, fenced inference and human-only batch decisions."""

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
    "'training-dataset'", "'active-learning'"
).replace("/datasets/", "/active-learning/")

STATUSES = "'queued','running','cancelling','lost','succeeded','failed','cancelled'"


class ActiveLearningRound(Base):
    __tablename__ = "active_learning_rounds"
    __table_args__ = (
        CheckConstraint(_STORAGE_REF_CHECK, name="ck_active_learning_storage_ref"),
        CheckConstraint(
            "storage_ref IS NULL OR storage_ref->>'prefix' = "
            "'projects/' || project_id::text || '/active-learning/' || id::text || '/'",
            name="ck_active_learning_storage_prefix",
        ),
        UniqueConstraint("project_id", "request_id", name="uq_active_learning_request"),
        Index("ix_active_learning_project_created", "project_id", "created_at"),
        CheckConstraint(f"status IN ({STATUSES})", name="ck_active_learning_status"),
        CheckConstraint(
            "inference_dispatches >= 0 AND accepted_tasks >= 0",
            name="ck_active_learning_counters",
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
    inference_dispatches: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    accepted_tasks: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
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


class ActiveLearningAttempt(Base):
    __tablename__ = "active_learning_attempts"
    __table_args__ = (
        UniqueConstraint(
            "round_id", "ordinal", name="uq_active_learning_attempt_ordinal"
        ),
        CheckConstraint(
            "ordinal > 0 AND generation > 0",
            name="ck_active_learning_attempt_generation",
        ),
        CheckConstraint(
            f"status IN ({STATUSES})", name="ck_active_learning_attempt_status"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    round_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("active_learning_rounds.id", ondelete="CASCADE"),
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


class ActiveLearningPoolMember(Base):
    __tablename__ = "active_learning_pool_members"

    round_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("active_learning_rounds.id", ondelete="CASCADE"),
        primary_key=True,
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tasks.id", ondelete="RESTRICT"),
        primary_key=True,
        index=True,
    )
    dataset_item_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dataset_items.id", ondelete="RESTRICT"),
        index=True,
    )


class ActiveLearningCandidate(Base):
    __tablename__ = "active_learning_candidates"
    __table_args__ = (
        CheckConstraint(
            "ordinal >= 0 AND ordinal < 32", name="ck_active_learning_candidate_ordinal"
        ),
        CheckConstraint(
            "status IN ('pending','inflight','succeeded','failed')",
            name="ck_active_learning_candidate_status",
        ),
        CheckConstraint(
            "disposition IS NULL OR disposition IN ('accepted','rejected')",
            name="ck_active_learning_disposition",
        ),
    )

    round_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("active_learning_rounds.id", ondelete="CASCADE"),
        primary_key=True,
    )
    ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tasks.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    dataset_item_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dataset_items.id", ondelete="RESTRICT"),
        index=True,
    )
    request_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, unique=True
    )
    status: Mapped[str] = mapped_column(String(24), default="pending", nullable=False)
    request: Mapped[dict | None] = mapped_column(JSONB)
    media: Mapped[dict | None] = mapped_column(JSONB)
    evidence: Mapped[dict | None] = mapped_column(JSONB)
    produced_attempt_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    disposition: Mapped[str | None] = mapped_column(String(16))
    decision_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    error_code: Mapped[str | None] = mapped_column(String(80))
    error_message: Mapped[str | None] = mapped_column(String(1000))


class ActiveLearningDecision(Base):
    __tablename__ = "active_learning_decisions"
    __table_args__ = (
        UniqueConstraint(
            "round_id", "request_id", name="uq_active_learning_decision_request"
        ),
        CheckConstraint(
            "action IN ('accept','reject')", name="ck_active_learning_decision_action"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    round_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("active_learning_rounds.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    request_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    accepted_batch_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("task_batches.id", ondelete="SET NULL")
    )
    snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    snapshot_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ActiveLearningReference(Base):
    __tablename__ = "active_learning_references"
    __table_args__ = (
        CheckConstraint(
            "(dataset_version_id IS NULL) <> (evaluation_id IS NULL)",
            name="ck_active_learning_reference_source",
        ),
        UniqueConstraint(
            "round_id", "dataset_version_id", name="uq_active_learning_dataset_ref"
        ),
        UniqueConstraint(
            "round_id", "evaluation_id", name="uq_active_learning_evaluation_ref"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    round_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("active_learning_rounds.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    dataset_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("training_dataset_versions.id", ondelete="RESTRICT"),
        index=True,
    )
    evaluation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sample_evaluation_runs.id", ondelete="RESTRICT"),
        index=True,
    )
    source_digest: Mapped[str] = mapped_column(String(64), nullable=False)


class ActiveLearningFeedback(Base):
    __tablename__ = "active_learning_feedback"
    __table_args__ = (
        UniqueConstraint(
            "round_id", "request_id", name="uq_active_learning_feedback_request"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    round_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("active_learning_rounds.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    request_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    snapshot_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
