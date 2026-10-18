from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class DatasetImportOperation(Base):
    """Assets-owned retained acceptance, lease, and aggregate import state."""

    __tablename__ = "dataset_import_operations"
    __table_args__ = (
        CheckConstraint(
            "connection_kind IN ('s3', 'sftp')",
            name="ck_dataset_import_operations_connection_kind",
        ),
        CheckConstraint(
            "connection_scope IN ('global', 'owner')",
            name="ck_dataset_import_operations_connection_scope",
        ),
        CheckConstraint(
            "stage IN ('enumerating', 'importing', 'completed', 'failed', 'cancelled')",
            name="ck_dataset_import_operations_stage",
        ),
        CheckConstraint(
            "jsonb_typeof(connection_config) = 'object'",
            name="ck_dataset_import_operations_connection_config_object",
        ),
        CheckConstraint(
            "jsonb_typeof(request) = 'object'",
            name="ck_dataset_import_operations_request_object",
        ),
        CheckConstraint(
            "jsonb_typeof(settings_snapshot) = 'object'",
            name="ck_dataset_import_operations_settings_snapshot_object",
        ),
        CheckConstraint(
            "octet_length(connection_secret_enc) BETWEEN 1 AND 49152",
            name="ck_dataset_import_operations_secret_size",
        ),
        CheckConstraint(
            "(lease_token IS NULL) = (lease_expires_at IS NULL)",
            name="ck_dataset_import_operations_lease_pair",
        ),
        CheckConstraint(
            "(total IS NULL) = (total_bytes IS NULL)",
            name="ck_dataset_import_operations_manifest_pair",
        ),
        CheckConstraint(
            "cursor = added + skipped + error_count",
            name="ck_dataset_import_operations_outcome_cursor",
        ),
        CheckConstraint(
            "jsonb_typeof(errors) = 'array' AND jsonb_array_length(errors) <= 100",
            name="ck_dataset_import_operations_errors_shape",
        ),
        CheckConstraint(
            "jsonb_array_length(errors) <= error_count",
            name="ck_dataset_import_operations_errors_count",
        ),
        CheckConstraint("cursor >= 0", name="ck_dataset_import_operations_cursor"),
        CheckConstraint(
            "added >= 0 AND skipped >= 0 AND error_count >= 0 AND linked_tasks >= 0",
            name="ck_dataset_import_operations_counts",
        ),
        CheckConstraint(
            "committed_bytes >= 0",
            name="ck_dataset_import_operations_committed_bytes",
        ),
        CheckConstraint(
            "total IS NULL OR (cursor <= total AND total >= 0)",
            name="ck_dataset_import_operations_total",
        ),
        CheckConstraint(
            "total_bytes IS NULL OR total_bytes >= 0",
            name="ck_dataset_import_operations_total_bytes",
        ),
        CheckConstraint(
            "stage NOT IN ('completed', 'failed', 'cancelled') OR lease_token IS NULL",
            name="ck_dataset_import_operations_terminal_lease",
        ),
        CheckConstraint(
            "stage <> 'enumerating' OR (cursor = 0 AND total IS NULL)",
            name="ck_dataset_import_operations_enumerating",
        ),
        CheckConstraint(
            "stage <> 'importing' OR total IS NOT NULL",
            name="ck_dataset_import_operations_importing",
        ),
        CheckConstraint(
            "stage <> 'completed' OR (total IS NOT NULL AND cursor = total)",
            name="ck_dataset_import_operations_completed",
        ),
        Index(
            "ix_dataset_import_operations_claim",
            "created_at",
            "job_id",
            postgresql_where=text("stage IN ('enumerating', 'importing')"),
        ),
        Index(
            "ix_dataset_import_operations_dataset_stage",
            "dataset_id",
            "stage",
            "created_at",
        ),
    )

    job_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    actor_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    dataset_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    connection_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    connection_name: Mapped[str] = mapped_column(String(255), nullable=False)
    connection_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    connection_scope: Mapped[str] = mapped_column(String(20), nullable=False)
    connection_created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    connection_updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    connection_config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    connection_secret_enc: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    request: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    settings_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    stage: Mapped[str] = mapped_column(String(16), nullable=False)
    cursor: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    added: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    skipped: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    error_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    errors: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default="[]"
    )
    linked_tasks: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    committed_bytes: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    lease_token: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class DatasetImportEntry(Base):
    """Immutable manifest row and resumable settlement receipt for one source."""

    __tablename__ = "dataset_import_entries"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'added', 'skipped', 'error')",
            name="ck_dataset_import_entries_status",
        ),
        CheckConstraint("entry_index >= 0", name="ck_dataset_import_entries_index"),
        CheckConstraint(
            "jsonb_typeof(source) = 'object'",
            name="ck_dataset_import_entries_source_object",
        ),
        CheckConstraint(
            "file_type IN ('image', 'video', 'point_cloud', 'other')",
            name="ck_dataset_import_entries_file_type",
        ),
        CheckConstraint(
            "(publication_id IS NULL) = (file_path IS NULL)",
            name="ck_dataset_import_entries_publication_pair",
        ),
        CheckConstraint(
            "(width IS NULL) = (height IS NULL)",
            name="ck_dataset_import_entries_dimension_pair",
        ),
        CheckConstraint(
            "file_size IS NULL OR file_size >= 0",
            name="ck_dataset_import_entries_file_size",
        ),
        CheckConstraint(
            "width IS NULL OR width > 0",
            name="ck_dataset_import_entries_width",
        ),
        CheckConstraint(
            "height IS NULL OR height > 0",
            name="ck_dataset_import_entries_height",
        ),
        CheckConstraint(
            "content_hash IS NULL OR content_hash ~ '^[0-9a-f]{32}$'",
            name="ck_dataset_import_entries_content_hash",
        ),
        CheckConstraint(
            "error_code IS NULL OR error_code ~ '^[a-z][a-z0-9_]{0,63}$'",
            name="ck_dataset_import_entries_error_code",
        ),
        CheckConstraint(
            "linked_tasks >= 0", name="ck_dataset_import_entries_linked_tasks"
        ),
        CheckConstraint(
            "status <> 'pending' OR (file_size IS NULL AND content_hash IS NULL AND error_code IS NULL AND settled_at IS NULL)",
            name="ck_dataset_import_entries_pending_result",
        ),
        CheckConstraint(
            "status <> 'error' OR (error_code IS NOT NULL AND settled_at IS NOT NULL)",
            name="ck_dataset_import_entries_error_result",
        ),
        CheckConstraint(
            "status NOT IN ('added', 'skipped') OR (file_size IS NOT NULL AND content_hash IS NOT NULL AND settled_at IS NOT NULL)",
            name="ck_dataset_import_entries_success_result",
        ),
        UniqueConstraint("job_id", "item_id", name="uq_dataset_import_entries_item"),
        UniqueConstraint(
            "job_id", "publication_id", name="uq_dataset_import_entries_publication"
        ),
        Index("ix_dataset_import_entries_item", "item_id"),
    )

    job_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    entry_index: Mapped[int] = mapped_column(Integer, primary_key=True)
    item_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    source: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    file_name: Mapped[str | None] = mapped_column(String(500), nullable=True)
    file_type: Mapped[str] = mapped_column(String(20), nullable=False)
    publication_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    file_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="pending"
    )
    file_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(32), nullable=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    linked_tasks: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    settled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
