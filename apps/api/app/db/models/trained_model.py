"""Immutable trained artifacts, installation receipts, and project releases."""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class TrainedModelVersion(Base):
    __tablename__ = "trained_model_versions"
    __table_args__ = (
        UniqueConstraint("project_id", "request_id", name="uq_trained_model_request"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="RESTRICT"), index=True
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("training_runs.id", ondelete="RESTRICT"),
        index=True,
    )
    attempt_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("training_attempts.id", ondelete="RESTRICT")
    )
    creator_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    request_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    request_digest: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(120))
    framework: Mapped[str] = mapped_column(String(30))
    framework_version: Mapped[str] = mapped_column(String(50))
    artifact: Mapped[dict] = mapped_column(JSONB)
    classes: Mapped[list] = mapped_column(JSONB)
    bindings: Mapped[list] = mapped_column(JSONB)
    binding_digest: Mapped[str] = mapped_column(String(64))
    provenance: Mapped[dict] = mapped_column(JSONB)
    evaluation: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ModelDeployment(Base):
    __tablename__ = "trained_model_deployments"
    __table_args__ = (
        UniqueConstraint(
            "model_version_id", "registry_id", name="uq_trained_model_deployment"
        ),
        CheckConstraint(
            "status IN ('checking','ready','failed')",
            name="ck_trained_model_deployment_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    model_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("trained_model_versions.id", ondelete="RESTRICT"),
        index=True,
    )
    registry_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ml_backend_registry.id", ondelete="RESTRICT"),
        index=True,
    )
    check_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), default=uuid.uuid4)
    status: Mapped[str] = mapped_column(String(20), default="checking")
    receipt: Mapped[dict] = mapped_column(JSONB, default=dict)
    trial: Mapped[dict] = mapped_column(JSONB, default=dict)
    error_message: Mapped[str | None] = mapped_column(String(1000))
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ModelRelease(Base):
    __tablename__ = "project_model_releases"
    __table_args__ = (
        UniqueConstraint("project_id", "request_id", name="uq_model_release_request"),
        UniqueConstraint(
            "project_id", "task", "generation", name="uq_model_release_generation"
        ),
        CheckConstraint("task IN ('detect','segment')", name="ck_model_release_task"),
        CheckConstraint("generation > 0", name="ck_model_release_generation"),
        CheckConstraint(
            "(model_version_id IS NULL) = (deployment_id IS NULL)",
            name="ck_model_release_pair",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="RESTRICT"), index=True
    )
    model_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("trained_model_versions.id", ondelete="RESTRICT")
    )
    deployment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("trained_model_deployments.id", ondelete="RESTRICT"),
    )
    creator_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    previous_release_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_model_releases.id", ondelete="RESTRICT")
    )
    rollback_from_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_model_releases.id", ondelete="RESTRICT")
    )
    request_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    request_digest: Mapped[str] = mapped_column(String(64))
    generation: Mapped[int] = mapped_column(Integer)
    task: Mapped[str] = mapped_column(
        String(20), default="detect", server_default="detect"
    )
    snapshot: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ProjectModelSelection(Base):
    __tablename__ = "project_model_selections"
    __table_args__ = (
        CheckConstraint("task IN ('detect','segment')", name="ck_model_selection_task"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    task: Mapped[str] = mapped_column(
        String(20), primary_key=True, default="detect", server_default="detect"
    )
    generation: Mapped[int] = mapped_column(Integer, default=0)
    release_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_model_releases.id", ondelete="RESTRICT")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
