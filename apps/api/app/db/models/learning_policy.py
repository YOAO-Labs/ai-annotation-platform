"""Opt-in automatic training and durable, auditable trigger identities."""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
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


class LearningPolicy(Base):
    __tablename__ = "learning_policies"
    __table_args__ = (
        UniqueConstraint("project_id", "task", name="uq_learning_policy_task"),
        CheckConstraint(
            "task IN ('detect', 'segment')", name="ck_learning_policy_task"
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
    task: Mapped[str] = mapped_column(
        String(20), default="detect", server_default="detect", nullable=False
    )
    creator_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    initialization_model_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "trained_model_versions.id",
            name="fk_learning_policy_initialization_model",
            ondelete="RESTRICT",
        ),
        index=True,
    )
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    config: Mapped[dict] = mapped_column(JSONB, nullable=False)
    authorization: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    baseline_dataset_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("training_dataset_versions.id", ondelete="RESTRICT"),
    )
    baseline: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    blocked_train_digest: Mapped[str | None] = mapped_column(String(64))
    last_snapshot_digest: Mapped[str | None] = mapped_column(String(64))
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    state: Mapped[str] = mapped_column(String(32), default="disabled", nullable=False)
    message: Mapped[str | None] = mapped_column(String(1000))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class LearningTrigger(Base):
    __tablename__ = "learning_triggers"
    __table_args__ = (
        UniqueConstraint(
            "policy_id",
            "policy_revision",
            "candidate_digest",
            name="uq_learning_candidate",
        ),
        Index("ix_learning_trigger_project_created", "project_id", "created_at"),
        Index(
            "uq_learning_active_project",
            "project_id",
            unique=True,
            postgresql_where=text("status IN ('building','queued','running')"),
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    policy_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("learning_policies.id", ondelete="CASCADE"),
        nullable=False,
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="RESTRICT"),
        nullable=False,
    )
    policy_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    dataset_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("training_dataset_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(24), default="building", nullable=False)
    source_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    candidate_digest: Mapped[str | None] = mapped_column(String(64))
    train_digest: Mapped[str | None] = mapped_column(String(64))
    validation_digest: Mapped[str | None] = mapped_column(String(64))
    changed_samples: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    message: Mapped[str | None] = mapped_column(String(1000))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
