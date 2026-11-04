from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AnnotationOperation(Base):
    """Durable idempotency and audit summary for one atomic annotation mutation."""

    __tablename__ = "annotation_operations"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('split_components', 'copy_component', 'copy_keyframe', "
            "'join_masks', 'overlap', 'convert_annotations', "
            "'delete_small_islands', 'fill_small_holes', "
            "'resolve_same_class_overlap', 'mask_repair_rollback', "
            "'slice_polygon', 'restore_slice', 'slice_mask', "
            "'create_annotation', 'accept_prediction', 'restore_acceptance')",
            name="ck_annotation_operations_kind",
        ),
        CheckConstraint(
            "status IN ('committed')",
            name="ck_annotation_operations_status",
        ),
        UniqueConstraint(
            "task_id",
            "actor_id",
            "idempotency_key",
            name="uq_annotation_operations_task_actor_key",
        ),
        Index("ix_annotation_operations_task_created", "task_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Durable soft reference: deleting/deactivating a user must not erase the
    # idempotency and lineage ledger.
    actor_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    scope_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    source_versions: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    result_versions: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    report: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="committed", server_default="committed"
    )
    response_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AnnotationLineageEdge(Base):
    """Many-to-many lineage edge; annotation IDs intentionally remain soft refs."""

    __tablename__ = "annotation_lineage_edges"
    __table_args__ = (
        CheckConstraint(
            "source_annotation_id IS NOT NULL OR result_annotation_id IS NOT NULL",
            name="ck_annotation_lineage_has_endpoint",
        ),
        CheckConstraint(
            "source_version IS NULL OR source_version >= 1",
            name="ck_annotation_lineage_source_version",
        ),
        CheckConstraint(
            "result_version IS NULL OR result_version >= 1",
            name="ck_annotation_lineage_result_version",
        ),
        CheckConstraint(
            "frame_index IS NULL OR frame_index >= 0",
            name="ck_annotation_lineage_frame_index",
        ),
        CheckConstraint(
            "relation IN ('split', 'copied', 'keyframe_copied', 'joined', "
            "'overlap_erased', 'converted', 'mask_repaired', "
            "'mask_repair_rolled_back', 'slice_restored')",
            name="ck_annotation_lineage_relation",
        ),
        Index("ix_annotation_lineage_source", "source_annotation_id"),
        Index("ix_annotation_lineage_result", "result_annotation_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    operation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("annotation_operations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source_annotation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    result_annotation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    relation: Mapped[str] = mapped_column(String(40), nullable=False)
    source_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    result_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    frame_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
