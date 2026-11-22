import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class VideoTrackerJobStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    PENDING_REVIEW = "pending_review"
    PARTIALLY_REVIEWED = "partially_reviewed"
    ACCEPTED = "accepted"
    DISCARDED = "discarded"


class VideoTrackerJobKind(str, enum.Enum):
    TRACKING = "tracking"
    CORRECTION = "correction"


class VideoTrackerJob(Base):
    __tablename__ = "video_tracker_jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    dataset_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dataset_items.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    annotation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("annotations.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    segment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("video_segments.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    target_class_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    target_tool_unit_id: Mapped[str | None] = mapped_column(String(30), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=VideoTrackerJobStatus.QUEUED.value
    )
    job_kind: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=VideoTrackerJobKind.TRACKING.value,
        server_default=VideoTrackerJobKind.TRACKING.value,
    )
    track_id_snapshot: Mapped[str | None] = mapped_column(String(64), nullable=True)
    correction_frame: Mapped[int | None] = mapped_column(Integer, nullable=True)
    model_key: Mapped[str] = mapped_column(String(80), nullable=False)
    direction: Mapped[str] = mapped_column(String(20), nullable=False)
    from_frame: Mapped[int] = mapped_column(Integer, nullable=False)
    to_frame: Mapped[int] = mapped_column(Integer, nullable=False)
    prompt: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    staged_result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    revision: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    event_channel: Mapped[str] = mapped_column(String(160), nullable=False)
    celery_task_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "job_kind IN ('tracking', 'correction')",
            name="ck_video_tracker_jobs_kind",
        ),
        CheckConstraint(
            "(job_kind = 'tracking' AND correction_frame IS NULL) OR "
            "(job_kind = 'correction' AND annotation_id IS NOT NULL "
            "AND track_id_snapshot IS NOT NULL AND correction_frame IS NOT NULL "
            "AND correction_frame >= from_frame AND correction_frame <= to_frame)",
            name="ck_video_tracker_jobs_correction_shape",
        ),
        Index("ix_video_tracker_jobs_task_status", "task_id", "status"),
        Index(
            "ix_video_tracker_jobs_dataset_frames",
            "dataset_item_id",
            "from_frame",
            "to_frame",
        ),
        Index(
            "uq_video_tracker_jobs_active_correction_track",
            "task_id",
            "track_id_snapshot",
            unique=True,
            postgresql_where=text(
                "job_kind = 'correction' AND status IN "
                "('queued', 'running', 'pending_review', 'partially_reviewed')"
            ),
        ),
    )
