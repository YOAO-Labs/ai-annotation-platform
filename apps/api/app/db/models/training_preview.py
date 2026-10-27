"""Immutable preview inputs and an independently fenced CPU slot."""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models.training_dataset import TRAINING_STORAGE_REF_CHECK

_STORAGE_REF_CHECK = TRAINING_STORAGE_REF_CHECK.replace(
    "'training-dataset'", "'training-preview'"
).replace("/datasets/", "/previews/")


class TrainingPreview(Base):
    __tablename__ = "training_previews"
    __table_args__ = (
        CheckConstraint(_STORAGE_REF_CHECK, name="ck_training_preview_storage_ref"),
        CheckConstraint(
            "storage_ref IS NULL OR storage_ref->>'prefix' = COALESCE(storage_ref->>'key_prefix','') || "
            "'projects/' || project_id::text || '/previews/' || id::text || '/'",
            name="ck_training_preview_storage_prefix",
        ),
        UniqueConstraint(
            "project_id", "request_id", name="uq_training_preview_request"
        ),
        Index("ix_training_preview_expiry", "expires_at"),
        Index("ix_training_preview_queue", "status", "created_at"),
        CheckConstraint(
            "status IN ('queued','running','cancelling','lost','succeeded','failed','cancelled','expired')",
            name="ck_training_preview_status",
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
    dataset_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("training_dataset_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    creator_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    async_job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("async_jobs.id", ondelete="SET NULL")
    )
    request_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    request: Mapped[dict] = mapped_column(JSONB, nullable=False)
    input_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    spec: Mapped[dict] = mapped_column(JSONB, nullable=False)
    storage_ref: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    authorization: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="queued")
    resource_id: Mapped[str] = mapped_column(String(200), nullable=False)
    executor_id: Mapped[str | None] = mapped_column(String(100))
    boot_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    claim_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    lease_digest: Mapped[str | None] = mapped_column(String(64))
    finish_digest: Mapped[str | None] = mapped_column(String(64))
    runtime: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    upload_inventory: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    artifacts: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    error_message: Mapped[str | None] = mapped_column(String(1000))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    put_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class TrainingPreviewResourceLease(Base):
    __tablename__ = "training_preview_resource_leases"

    resource_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    preview_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("training_previews.id", ondelete="RESTRICT"),
        unique=True,
    )
    executor_id: Mapped[str | None] = mapped_column(String(100))
    boot_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    runtime: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
