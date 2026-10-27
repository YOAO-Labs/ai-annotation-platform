from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models.mask_format_upload import MASK_BATCH_REF_CHECK


class MaskFormatImport(Base):
    __tablename__ = "mask_format_imports"
    __table_args__ = (
        CheckConstraint(
            "status IN ('staged', 'pending', 'running', 'partial', 'completed', "
            "'failed', 'cancelled')",
            name="ck_mask_format_imports_status",
        ),
        CheckConstraint(
            "staged_sha256 ~ '^[0-9a-f]{64}$' AND mapping_digest ~ '^[0-9a-f]{64}$' "
            "AND options_digest ~ '^[0-9a-f]{64}$' AND plan_digest ~ '^[0-9a-f]{64}$' "
            "AND token_hash ~ '^[0-9a-f]{64}$'",
            name="ck_mask_format_imports_digests",
        ),
        Index("ix_mask_format_imports_project_status", "project_id", "status"),
        Index("ix_mask_format_imports_receipt_expiry", "receipt_expires_at"),
        CheckConstraint(
            "staging_ref IS NULL OR (" + MASK_BATCH_REF_CHECK + ")",
            name="ck_mask_format_import_staging_ref",
        ),
        Index("ix_mask_format_import_location", text("(staging_ref->>'location_id')")),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    requested_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    async_job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("async_jobs.id", ondelete="SET NULL"),
        nullable=True,
        unique=True,
    )
    format_id: Mapped[str] = mapped_column(String(80), nullable=False)
    adapter_version: Mapped[str] = mapped_column(String(40), nullable=False)
    manifest_version: Mapped[str] = mapped_column(String(40), nullable=False)
    staged_object_key: Mapped[str] = mapped_column(String(500), nullable=False)
    staging_ref: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    staged_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    mapping_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    options_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    mapping_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    options_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    plan_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    plan_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    receipt_expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="staged", server_default="staged"
    )
    result_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
