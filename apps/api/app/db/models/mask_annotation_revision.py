from __future__ import annotations

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


class MaskAnnotationRevision(Base):
    """Immutable geometry snapshot captured before a Mask annotation changes."""

    __tablename__ = "mask_annotation_revisions"
    __table_args__ = (
        CheckConstraint(
            "annotation_version >= 1",
            name="ck_mask_annotation_revisions_version",
        ),
        CheckConstraint(
            "geometry_digest ~ '^[0-9a-f]{64}$'",
            name="ck_mask_annotation_revisions_geometry_digest",
        ),
        UniqueConstraint(
            "annotation_id",
            "annotation_version",
            name="uq_mask_annotation_revisions_annotation_version",
        ),
        Index(
            "ix_mask_annotation_revisions_project_expires",
            "project_id",
            "expires_at",
        ),
        Index(
            "ix_mask_annotation_revisions_task_created",
            "task_id",
            "created_at",
        ),
        Index("ix_mask_annotation_revisions_expires_at", "expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
    )
    annotation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    annotation_version: Mapped[int] = mapped_column(Integer, nullable=False)
    geometry: Mapped[dict] = mapped_column(JSONB, nullable=False)
    geometry_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    source_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    operation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("'infinity'::timestamptz"),
    )
