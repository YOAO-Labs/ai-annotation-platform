from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class TaskCreationOperation(Base):
    """Durable accepted plan for one initial dataset task-creation operation."""

    __tablename__ = "task_creation_operations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    actor_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    dataset_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    association_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    mode: Mapped[str] = mapped_column(String(20), nullable=False)
    chunk_size: Mapped[int] = mapped_column(Integer, nullable=False)
    request: Mapped[dict] = mapped_column(JSONB, nullable=False)
    manifest: Mapped[list | None] = mapped_column(JSONB)
    total: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    next_chunk: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    processed: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    created: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    state: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="accepted"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        CheckConstraint(
            "mode IN ('generic', 'pointcloud')",
            name="ck_task_creation_operations_mode",
        ),
        CheckConstraint(
            "chunk_size BETWEEN 1 AND 5000",
            name="ck_task_creation_operations_chunk_size",
        ),
        CheckConstraint(
            "state IN ('accepted', 'running', 'completed')",
            name="ck_task_creation_operations_state",
        ),
        CheckConstraint(
            "next_chunk >= 0 AND processed >= 0 AND created >= 0",
            name="ck_task_creation_operations_progress_nonnegative",
        ),
        CheckConstraint(
            "total IS NULL OR total >= 0", name="ck_task_creation_operations_total"
        ),
    )
