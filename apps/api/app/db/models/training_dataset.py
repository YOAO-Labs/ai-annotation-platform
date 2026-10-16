"""Durable immutable training input, independent of transient export jobs."""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    case,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    literal,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, column_property

from app.db.base import Base


class TrainingDatasetVersion(Base):
    __tablename__ = "training_dataset_versions"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "request_id", name="uq_training_dataset_request"
        ),
        CheckConstraint(
            "status IN ('pending','running','ready','failed','cancelled')",
            name="ck_training_dataset_status",
        ),
        Index("ix_training_dataset_project_created", "project_id", "created_at"),
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
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("training_dataset_versions.id", ondelete="RESTRICT"),
    )
    async_job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("async_jobs.id", ondelete="SET NULL")
    )
    request_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    attempt_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), default=uuid.uuid4, nullable=False
    )
    snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    task = column_property(
        case(
            (
                snapshot["task"]
                .as_string()
                .in_(["image-instance-segmentation", "segment"]),
                "segment",
            ),
            (snapshot["task"].as_string() == "image-detection", "detect"),
            else_=func.coalesce(snapshot["task"].as_string(), "detect"),
        )
    )
    media_type = column_property(
        func.coalesce(snapshot["media_type"].as_string(), "image")
    )
    format = column_property(
        func.coalesce(snapshot["format"].as_string(), "ultralytics-yolo")
    )
    source_media_type = column_property(
        func.coalesce(snapshot["source"]["media_type"].as_string(), "image")
    )
    snapshot_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    class_count = column_property(
        func.coalesce(func.jsonb_array_length(snapshot["classes"]), 0)
    )
    annotation_scope = column_property(
        func.coalesce(snapshot["annotation_scope"], literal({}, type_=JSONB))
    )
    preparation_report: Mapped[dict | None] = mapped_column(JSONB)
    summary = column_property(
        func.coalesce(
            manifest["summary"], preparation_report["summary"], literal({}, type_=JSONB)
        )
    )
    progress_pct: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(80))
    error_message: Mapped[str | None] = mapped_column(String(1000))
    package_key: Mapped[str | None] = mapped_column(String(500))
    package_sha256: Mapped[str | None] = mapped_column(String(64))
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
