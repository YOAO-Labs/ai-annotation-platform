"""Durable finite searches retain immutable trial plans and ordinary run owners."""

import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class TrainingStudy(Base):
    __tablename__ = "training_studies"
    __table_args__ = (
        UniqueConstraint("project_id", "request_id", name="uq_training_study_request"),
        Index("ix_training_study_project_created", "project_id", "created_at"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT")
    )
    creator_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    dataset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("training_dataset_versions.id", ondelete="RESTRICT")
    )
    initialization_model_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("trained_model_versions.id", ondelete="RESTRICT")
    )
    request_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    request_digest: Mapped[str] = mapped_column(String(64))
    plan_digest: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(24), default="active")
    scheduling: Mapped[bool] = mapped_column(default=True)
    plan: Mapped[dict] = mapped_column(JSONB)
    authorization: Mapped[dict] = mapped_column(JSONB, default=dict)
    origin: Mapped[dict | None] = mapped_column(JSONB)
    message: Mapped[str | None] = mapped_column(String(1000))
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TrainingTrial(Base):
    __tablename__ = "training_trials"
    __table_args__ = (
        UniqueConstraint("study_id", "ordinal", name="uq_training_trial_ordinal"),
        UniqueConstraint("run_id", name="uq_training_trial_run"),
        ForeignKeyConstraint(
            ["run_id", "study_id"],
            ["training_runs.id", "training_runs.study_id"],
            name="fk_training_trial_same_study_run",
            ondelete="RESTRICT",
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    study_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("training_studies.id", ondelete="RESTRICT")
    )
    ordinal: Mapped[int] = mapped_column(Integer)
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    config: Mapped[dict] = mapped_column(JSONB)
    config_digest: Mapped[str] = mapped_column(String(64))
    values: Mapped[dict] = mapped_column(JSONB)
