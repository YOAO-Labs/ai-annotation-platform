import enum
import uuid
from datetime import datetime
from sqlalchemy import (
    String,
    Boolean,
    BigInteger,
    Integer,
    Float,
    DateTime,
    ForeignKey,
    func,
    Index,
    text,
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base


class RejectReasonType(str, enum.Enum):
    """reviewer 驳回结构化枚举。收紧到 4 类，前端 4 个预设按钮一一映射。"""

    MISSING = "missing"
    EXTRA = "extra"
    WRONG_LABEL = "wrong_label"
    WRONG_GEOMETRY = "wrong_geometry"


class Task(Base):
    __tablename__ = "tasks"

    __table_args__ = (
        Index("ix_tasks_project_created_id", "project_id", "created_at", "id"),
        Index(
            "ix_tasks_batch_unlabeled",
            "batch_id",
            postgresql_where=text("is_labeled = false"),
        ),
        Index(
            "ix_tasks_project_unbatched",
            "project_id",
            "created_at",
            "id",
            postgresql_where=text("batch_id IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), index=True
    )
    dataset_item_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("dataset_items.id"), index=True
    )
    display_id: Mapped[str] = mapped_column(String(30), nullable=False, unique=True)
    file_name: Mapped[str] = mapped_column(String(500), nullable=False)
    file_path: Mapped[str] = mapped_column(String(1000), nullable=False)
    file_type: Mapped[str] = mapped_column(String(20), default="image")
    media_source_generation: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=1, server_default="1"
    )
    media_object_identity: Mapped[dict | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    media_source_drifted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    tags: Mapped[list] = mapped_column(JSONB, default=list)
    status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    assignee_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id")
    )
    assignee_is_override: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    assigned_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    is_labeled: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    overlap: Mapped[int] = mapped_column(Integer, default=1)
    total_annotations: Mapped[int] = mapped_column(Integer, default=0)
    total_predictions: Mapped[int] = mapped_column(Integer, default=0)
    precomputed_agreement: Mapped[float | None] = mapped_column(Float)
    sequence_order: Mapped[int | None] = mapped_column(Integer)
    thumbnail_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    blurhash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    batch_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("task_batches.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    submitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reviewer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    reviewer_is_override: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    reviewer_claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    review_round_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    first_review_eligible: Mapped[bool | None] = mapped_column(
        Boolean, nullable=True, default=True, server_default="true"
    )
    first_reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    first_review_result: Mapped[str | None] = mapped_column(String(16), nullable=True)
    first_review_contributor_ids: Mapped[list | None] = mapped_column(
        JSONB, nullable=True
    )
    annotation_contributor_ids: Mapped[list | None] = mapped_column(
        JSONB, nullable=True, default=list
    )
    review_contributor_ids: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    review_submitter_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reject_reason: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    reject_reason_type: Mapped[str | None] = mapped_column(
        String(20), nullable=True, index=True
    )
    skip_reason: Mapped[str | None] = mapped_column(String(50), nullable=True)
    skipped_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reopened_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_reopened_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    thumbnail_output_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("thumbnail_outputs.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    original_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("task_originals.id", ondelete="RESTRICT"),
        nullable=True,
    )
