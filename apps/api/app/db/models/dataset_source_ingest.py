from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class DatasetSourceIngestOperation(Base):
    """Assets-owned transaction-bound receipt for synchronous source ingest."""

    __tablename__ = "dataset_source_ingest_operations"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('zip', 'scan')", name="ck_dataset_source_ingest_operations_kind"
        ),
        CheckConstraint(
            "state IN ('preparing', 'completed')",
            name="ck_dataset_source_ingest_operations_state",
        ),
        CheckConstraint(
            "jsonb_typeof(descriptor) = 'object'",
            name="ck_dataset_source_ingest_operations_descriptor_object",
        ),
        CheckConstraint(
            "request_digest ~ '^[0-9a-f]{64}$'",
            name="ck_dataset_source_ingest_operations_request_digest",
        ),
        CheckConstraint(
            "next_index >= 0", name="ck_dataset_source_ingest_operations_next_index"
        ),
        CheckConstraint(
            "added >= 0 AND deduped >= 0 AND skipped >= 0 AND error_count >= 0 AND linked_tasks >= 0",
            name="ck_dataset_source_ingest_operations_counts",
        ),
        CheckConstraint(
            "(layout_snapshot_digest IS NULL) = (layout_scene_count IS NULL) AND (layout_scene_count IS NULL) = (layout_assigned_count IS NULL) AND (layout_assigned_count IS NULL) = (layout_total_items IS NULL)",
            name="ck_dataset_source_ingest_operations_layout_snapshot_pair",
        ),
        CheckConstraint(
            "layout_snapshot_digest IS NULL OR layout_snapshot_digest ~ '^[0-9a-f]{64}$'",
            name="ck_dataset_source_ingest_operations_layout_digest",
        ),
        CheckConstraint(
            "layout_scene_count IS NULL OR (layout_scene_count >= 0 AND layout_assigned_count >= 0 AND layout_total_items >= layout_assigned_count)",
            name="ck_dataset_source_ingest_operations_layout_counts",
        ),
        CheckConstraint(
            "(state = 'completed') = (result IS NOT NULL)",
            name="ck_dataset_source_ingest_operations_result_state",
        ),
        CheckConstraint(
            "result IS NULL OR (jsonb_typeof(result) = 'object' AND result ->> 'operation_id' = operation_id::text AND result ->> 'actor_id' = actor_id::text AND result ->> 'dataset_id' = dataset_id::text)",
            name="ck_dataset_source_ingest_operations_result_scope",
        ),
        Index(
            "ix_dataset_source_ingest_operations_dataset_state",
            "dataset_id",
            "state",
            "created_at",
        ),
    )

    operation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True
    )
    actor_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    dataset_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(String(8), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    descriptor: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    state: Mapped[str] = mapped_column(String(12), nullable=False)
    creator_transaction_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    dataset_name: Mapped[str] = mapped_column(String(255), nullable=False)
    is_temporal: Mapped[bool] = mapped_column(nullable=False)
    source_prefix: Mapped[str] = mapped_column(Text, nullable=False)
    next_index: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    added: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    deduped: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    skipped: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    error_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    linked_tasks: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    layout_snapshot_digest: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    layout_scene_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    layout_assigned_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    layout_total_items: Mapped[int | None] = mapped_column(Integer, nullable=True)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class DatasetSourceIngestEntry(Base):
    """Ordered native plan and immutable completion proof for one source object."""

    __tablename__ = "dataset_source_ingest_entries"
    __table_args__ = (
        CheckConstraint(
            "planned_status IN ('pending', 'deduped', 'skipped', 'error')",
            name="ck_dataset_source_ingest_entries_planned_status",
        ),
        CheckConstraint(
            "status IN ('pending', 'added', 'deduped', 'skipped', 'error')",
            name="ck_dataset_source_ingest_entries_status",
        ),
        CheckConstraint(
            "entry_index >= 0", name="ck_dataset_source_ingest_entries_index"
        ),
        CheckConstraint(
            "jsonb_typeof(source) = 'object'",
            name="ck_dataset_source_ingest_entries_source_object",
        ),
        CheckConstraint(
            "error_code IS NULL OR error_code IN ('invalid_source_object', 'archive_entry_read_failed', 'storage_write_failed', 'source_changed', 'source_missing', 'source_read_failed')",
            name="ck_dataset_source_ingest_entries_error_code",
        ),
        CheckConstraint(
            "(width IS NULL) = (height IS NULL)",
            name="ck_dataset_source_ingest_entries_dimension_pair",
        ),
        CheckConstraint(
            "file_size IS NULL OR file_size >= 0",
            name="ck_dataset_source_ingest_entries_file_size",
        ),
        CheckConstraint(
            "width IS NULL OR width > 0", name="ck_dataset_source_ingest_entries_width"
        ),
        CheckConstraint(
            "height IS NULL OR height > 0",
            name="ck_dataset_source_ingest_entries_height",
        ),
        CheckConstraint(
            "content_hash IS NULL OR content_hash ~ '^[0-9a-f]{32}$'",
            name="ck_dataset_source_ingest_entries_content_hash",
        ),
        CheckConstraint(
            "completion IS NULL OR jsonb_typeof(completion) = 'object'",
            name="ck_dataset_source_ingest_entries_completion_object",
        ),
        CheckConstraint(
            "status <> 'pending' OR (planned_status = 'pending' AND item_id IS NOT NULL AND file_name IS NOT NULL AND file_path IS NOT NULL AND settled_at IS NULL)",
            name="ck_dataset_source_ingest_entries_pending_plan",
        ),
        CheckConstraint(
            "status <> 'error' OR (error_code IS NOT NULL AND settled_at IS NOT NULL)",
            name="ck_dataset_source_ingest_entries_error_settled",
        ),
        CheckConstraint(
            "status NOT IN ('added', 'deduped', 'skipped') OR settled_at IS NOT NULL",
            name="ck_dataset_source_ingest_entries_nonerror_settled",
        ),
        ForeignKeyConstraint(
            ["operation_id"],
            ["dataset_source_ingest_operations.operation_id"],
            ondelete="CASCADE",
            name="fk_dataset_source_ingest_entries_operation",
        ),
        UniqueConstraint(
            "operation_id", "entry_index", name="uq_dataset_source_ingest_entries_index"
        ),
        UniqueConstraint(
            "operation_id", "item_id", name="uq_dataset_source_ingest_entries_item"
        ),
        UniqueConstraint(
            "operation_id",
            "publication_id",
            name="uq_dataset_source_ingest_entries_publication",
        ),
        Index(
            "ix_dataset_source_ingest_entries_batch",
            "operation_id",
            "batch_id",
            "entry_index",
        ),
    )

    operation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True
    )
    entry_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    entry_index: Mapped[int] = mapped_column(Integer, nullable=False)
    batch_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    source: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    planned_status: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    item_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    publication_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    file_name: Mapped[str | None] = mapped_column(String(500), nullable=True)
    file_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    completion: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    file_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(32), nullable=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    settled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class DatasetSourceIngestDelivery(Base):
    """At-least-once media handoff committed with a source ingest."""

    __tablename__ = "dataset_source_ingest_deliveries"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('thumbnail', 'video_metadata')",
            name="ck_dataset_source_ingest_deliveries_kind",
        ),
        CheckConstraint(
            "attempts >= 0", name="ck_dataset_source_ingest_deliveries_attempts"
        ),
        CheckConstraint(
            "(lease_token IS NULL) = (lease_expires_at IS NULL)",
            name="ck_dataset_source_ingest_deliveries_lease_pair",
        ),
        CheckConstraint(
            "delivered_at IS NULL OR lease_token IS NULL",
            name="ck_dataset_source_ingest_deliveries_delivered_lease",
        ),
        UniqueConstraint(
            "operation_id",
            "kind",
            "target_id",
            name="uq_dataset_source_ingest_deliveries_target",
        ),
        Index(
            "ix_dataset_source_ingest_deliveries_pending_created",
            "created_at",
            "id",
            postgresql_where=text("delivered_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("pg_catalog.gen_random_uuid()"),
    )
    operation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    target_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    lease_token: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    delivered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
