"""Independent recovery records for one prediction attempt's temporary inputs."""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, Index, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class PredictionInputLease(Base):
    __tablename__ = "prediction_input_leases"
    __table_args__ = (
        CheckConstraint(
            "state IN ('active', 'cleanup_pending')",
            name="ck_prediction_input_lease_state",
        ),
        Index("ix_prediction_input_cleanup", "next_cleanup_at", "expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    # Deliberately no cascading FKs: deleting a business owner cannot erase cleanup.
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    owner_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    bucket: Mapped[str] = mapped_column(String(255), nullable=False)
    storage_identity: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="active")
    uncertain_write: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    retain_until: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    next_cleanup_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_cleanup_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
