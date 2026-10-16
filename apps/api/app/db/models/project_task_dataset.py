"""Project-derived task streams; frozen versions retain their original authority."""

import uuid
from datetime import datetime

from aap_task_contract import VISUAL_TASK_VALUES
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, validates

from app.db.base import Base


class ProjectTaskDataset(Base):
    __tablename__ = "project_task_datasets"
    __table_args__ = (
        UniqueConstraint("project_id", "task", name="uq_project_task_dataset"),
        CheckConstraint("next_ordinal > 0", name="ck_task_dataset_next_ordinal"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="RESTRICT"),
        nullable=False,
    )
    task: Mapped[str] = mapped_column(String(32), nullable=False)
    next_ordinal: Mapped[int] = mapped_column(
        BigInteger, default=1, server_default="1", nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    @validates("task")
    def canonical_visual_task(self, _key, value):
        if value not in VISUAL_TASK_VALUES:
            raise ValueError("task dataset requires a canonical visual task")
        return value


class TaskDatasetVersionMembership(Base):
    __tablename__ = "task_dataset_version_memberships"
    __table_args__ = (
        UniqueConstraint("task_dataset_id", "ordinal", name="uq_task_dataset_ordinal"),
        CheckConstraint("ordinal > 0", name="ck_task_dataset_ordinal"),
    )

    version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("training_dataset_versions.id", ondelete="CASCADE"),
        primary_key=True,
    )
    task_dataset_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("project_task_datasets.id", ondelete="RESTRICT"),
        nullable=False,
    )
    ordinal: Mapped[int] = mapped_column(BigInteger, nullable=False)
