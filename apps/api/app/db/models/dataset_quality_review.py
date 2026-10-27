"""Immutable review scopes and transactional member checkpoints."""

from datetime import datetime
import uuid

from sqlalchemy import DateTime, ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class DatasetQualityReviewBatch(Base):
    __tablename__ = "dataset_quality_review_batches"

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    quality_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("dataset_quality_runs.id", ondelete="CASCADE"), index=True
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    async_job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("async_jobs.id", ondelete="SET NULL")
    )
    authority: Mapped[dict] = mapped_column(JSONB, default=dict)
    selection: Mapped[dict] = mapped_column(JSONB)
    proof: Mapped[dict] = mapped_column(JSONB)
    scope_digest: Mapped[str] = mapped_column(String(64))
    decision: Mapped[str] = mapped_column(String(20))
    note: Mapped[str] = mapped_column(String(2000))
    status: Mapped[str] = mapped_column(String(20), default="preview")
    total: Mapped[int] = mapped_column()
    actionable_count: Mapped[int] = mapped_column()
    historical_count: Mapped[int] = mapped_column()
    processed: Mapped[int] = mapped_column(default=0)
    summary: Mapped[dict] = mapped_column(JSONB, default=dict)
    generation: Mapped[int] = mapped_column(default=0)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DatasetQualityReviewMember(Base):
    __tablename__ = "dataset_quality_review_members"

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    batch_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("dataset_quality_review_batches.id", ondelete="CASCADE")
    )
    # The original identity survives disappearance of live quality evidence.
    result_id: Mapped[uuid.UUID] = mapped_column(UUID)
    snapshot: Mapped[dict] = mapped_column(JSONB)
    state: Mapped[str] = mapped_column(String(20), default="pending")
    reason_code: Mapped[str | None] = mapped_column(String(80))
    applied_revision: Mapped[int | None] = mapped_column()
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("batch_id", "result_id", name="uq_quality_review_member"),
        Index("ix_quality_review_member_state", "batch_id", "state", "id"),
    )
