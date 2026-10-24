"""Successful usage observations survive inventory attempts and job retention."""

from datetime import datetime
import uuid

from sqlalchemy import BigInteger, DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class StorageUsageSnapshot(Base):
    __tablename__ = "storage_usage_snapshots"

    scope_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    object_count: Mapped[int | None] = mapped_column(BigInteger)
    total_size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    category_totals: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    measurement_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    measured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt_status: Mapped[str] = mapped_column(String(20), default="never")
    attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(80))


class StorageUsageInventory(Base):
    __tablename__ = "storage_usage_inventories"

    id: Mapped[str] = mapped_column(String(20), primary_key=True)
    async_job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID, ForeignKey("async_jobs.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(String(20))
    authority: Mapped[dict] = mapped_column(JSONB)
    scope: Mapped[list] = mapped_column(JSONB)
    bucket_index: Mapped[int] = mapped_column(default=0)
    checkpoint: Mapped[dict] = mapped_column(JSONB, default=dict)
    success_count: Mapped[int] = mapped_column(default=0)
    failed_count: Mapped[int] = mapped_column(default=0)
    generation: Mapped[int] = mapped_column(default=0)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
