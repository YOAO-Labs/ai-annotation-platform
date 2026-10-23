"""Fixed quality membership and evidence, independent of task retention."""

from datetime import datetime
import uuid

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class DatasetQualityRun(Base):
    __tablename__ = "dataset_quality_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    dataset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("datasets.id", ondelete="CASCADE"), index=True
    )
    async_job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("async_jobs.id", ondelete="SET NULL")
    )
    authority: Mapped[dict] = mapped_column(JSONB)
    rule_version: Mapped[str] = mapped_column(String(40), default="image-quality-1")
    config: Mapped[dict] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    total: Mapped[int] = mapped_column(default=0)
    processed: Mapped[int] = mapped_column(default=0)
    summary: Mapped[dict] = mapped_column(JSONB, default=dict)
    generation: Mapped[int] = mapped_column(default=0)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index(
            "uq_dataset_quality_active",
            "dataset_id",
            unique=True,
            postgresql_where=text("status IN ('pending', 'running')"),
        ),
    )


class DatasetQualityResult(Base):
    __tablename__ = "dataset_quality_results"

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("dataset_quality_runs.id", ondelete="CASCADE"), index=True
    )
    # Historical UUIDs intentionally survive deletion of the live Item/Asset.
    item_id: Mapped[uuid.UUID] = mapped_column(UUID)
    asset_id: Mapped[uuid.UUID | None] = mapped_column(UUID)
    source_id: Mapped[uuid.UUID | None] = mapped_column(UUID)
    file_name: Mapped[str] = mapped_column(String(500))
    sha256: Mapped[str | None] = mapped_column(String(64))
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    state: Mapped[str] = mapped_column(String(20), default="pending")
    reason_code: Mapped[str | None] = mapped_column(String(80))
    bytes_verified: Mapped[bool] = mapped_column(default=False)
    findings: Mapped[list] = mapped_column(JSONB, default=list)
    width: Mapped[int | None] = mapped_column()
    height: Mapped[int | None] = mapped_column()
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_state: Mapped[str] = mapped_column(String(20), default="open")
    review_note: Mapped[str] = mapped_column(String(2000), default="")
    review_revision: Mapped[int] = mapped_column(default=0)
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("run_id", "item_id", name="uq_dataset_quality_member"),
        Index("ix_dataset_quality_result_state", "run_id", "state", "id"),
    )
