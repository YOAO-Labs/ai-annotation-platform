"""Disposable metadata indexes over immutable training inputs."""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class TrainingDatasetAnalysis(Base):
    __tablename__ = "training_dataset_analyses"

    version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("training_dataset_versions.id", ondelete="CASCADE"),
        primary_key=True,
    )
    input_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    summary: Mapped[dict] = mapped_column(JSONB, nullable=False)


class TrainingDatasetAnalyzedSample(Base):
    __tablename__ = "training_dataset_analyzed_samples"
    __table_args__ = (
        Index("ix_training_analysis_sample_split", "version_id", "split", "task_id"),
        Index("ix_training_analysis_sample_content", "version_id", "sha256"),
        Index(
            "ix_training_analysis_sample_classes", "class_ids", postgresql_using="gin"
        ),
    )

    version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("training_dataset_analyses.version_id", ondelete="CASCADE"),
        primary_key=True,
    )
    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    included: Mapped[bool] = mapped_column(Boolean, nullable=False)
    exclusion_reason: Mapped[str | None] = mapped_column(String(100))
    issue: Mapped[str | None] = mapped_column(String(100))
    sha256: Mapped[str | None] = mapped_column(String(64))
    label_sha256: Mapped[str | None] = mapped_column(String(64))
    semantic_label_digest: Mapped[str | None] = mapped_column(String(64))
    split: Mapped[str | None] = mapped_column(String(8))
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    instances: Mapped[int | None] = mapped_column(Integer)
    is_negative: Mapped[bool | None] = mapped_column(Boolean)
    explicit_negative: Mapped[bool | None] = mapped_column(Boolean)
    class_ids: Mapped[list] = mapped_column(JSONB, nullable=False)
    labels: Mapped[list] = mapped_column(JSONB, nullable=False)
    media_state: Mapped[str] = mapped_column(
        String(16), nullable=False, default="unchecked"
    )
    media_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
