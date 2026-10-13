import uuid
from datetime import datetime
from sqlalchemy import String, Boolean, Integer, DateTime, ForeignKey, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base


class TaskEvent(Base):
    """Closed Workbench annotation/review session interval.

    ``legacy``/``unverified_collection`` values keep pre-qualified rows out of
    effective-time metrics while retaining them for audit and migration.
    """

    __tablename__ = "task_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    ended_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    annotation_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0"
    )
    was_rejected: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    collector_version: Mapped[str | None] = mapped_column(
        String(32), nullable=True, default=None
    )
    collection_source: Mapped[str] = mapped_column(
        String(32), nullable=False, default="legacy", server_default="legacy"
    )
    collection_coverage: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="unverified_collection",
        server_default="unverified_collection",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
