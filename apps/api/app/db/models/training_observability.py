"""Bounded attempt observations; execution and lease authority live elsewhere."""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ObservationColumns:
    attempt_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("training_attempts.id", ondelete="CASCADE"),
        primary_key=True,
    )
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True)
    stream_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    producer_boot_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    content_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)


class TrainingTelemetrySample(ObservationColumns, Base):
    __tablename__ = "training_telemetry_samples"
    __table_args__ = (
        Index("ix_training_telemetry_retention", "received_at"),
        CheckConstraint("sequence > 0", name="ck_training_telemetry_samples_sequence"),
    )


class TrainingLogChunk(ObservationColumns, Base):
    __tablename__ = "training_log_chunks"
    __table_args__ = (
        Index("ix_training_log_retention", "received_at"),
        CheckConstraint("sequence > 0", name="ck_training_log_chunks_sequence"),
    )
