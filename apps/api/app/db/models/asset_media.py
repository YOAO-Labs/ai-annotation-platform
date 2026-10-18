"""Durable Assets-owned video work, staging, receipts and garbage leases."""

from datetime import datetime
import uuid

from sqlalchemy import (
    BigInteger,
    Boolean,
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


class AssetMediaJob(Base):
    __tablename__ = "asset_media_jobs"

    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    dataset_item_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    slot_key: Mapped[str] = mapped_column(String(255), nullable=False)
    generation: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    spec: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    source_generation: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )
    timetable_revision: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )
    actor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    progress: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_detail: Mapped[str | None] = mapped_column(Text)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_lease_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    last_outcome: Mapped[str | None] = mapped_column(String(24))
    accepted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    backfill_selection: Mapped[dict | None] = mapped_column(JSONB)
    backfill_cutoff: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    backfill_cursor: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))

    __table_args__ = (
        CheckConstraint(
            "kind IN ('video_metadata', 'video_timetable', 'frame', 'chunk', 'video_backfill')",
            name="ck_asset_media_jobs_kind",
        ),
        CheckConstraint(
            "status IN ('pending', 'running', 'ready', 'failed', 'canceled')",
            name="ck_asset_media_jobs_status",
        ),
        CheckConstraint("generation >= 1", name="ck_asset_media_jobs_generation"),
        CheckConstraint(
            "source_generation >= 0",
            name="ck_asset_media_jobs_source_generation",
        ),
        CheckConstraint(
            "timetable_revision >= 0",
            name="ck_asset_media_jobs_timetable_revision",
        ),
        CheckConstraint(
            "pg_catalog.jsonb_typeof(spec) = 'object'",
            name="ck_asset_media_jobs_spec_object",
        ),
        CheckConstraint(
            "pg_catalog.jsonb_typeof(progress) = 'object'",
            name="ck_asset_media_jobs_progress_object",
        ),
        CheckConstraint(
            "(lease_token IS NULL) = (lease_expires_at IS NULL)",
            name="ck_asset_media_jobs_lease_pair",
        ),
        CheckConstraint(
            "(last_lease_token IS NULL) = (last_outcome IS NULL)",
            name="ck_asset_media_jobs_last_outcome_pair",
        ),
        CheckConstraint(
            "(kind = 'video_backfill' AND dataset_item_id IS NULL "
            "AND backfill_selection IS NOT NULL AND backfill_cutoff IS NOT NULL) "
            "OR (kind <> 'video_backfill' AND dataset_item_id IS NOT NULL "
            "AND backfill_selection IS NULL AND backfill_cutoff IS NULL "
            "AND backfill_cursor IS NULL)",
            name="ck_asset_media_jobs_backfill_scope",
        ),
        Index(
            "uq_asset_media_jobs_item_slot",
            "dataset_item_id",
            "kind",
            "slot_key",
            unique=True,
            postgresql_where=text("dataset_item_id IS NOT NULL"),
        ),
        Index(
            "uq_asset_media_jobs_backfill_slot",
            "slot_key",
            unique=True,
            postgresql_where=text("dataset_item_id IS NULL"),
        ),
        Index(
            "ix_asset_media_jobs_claim",
            "status",
            "lease_expires_at",
            "accepted_at",
        ),
        Index(
            "ix_asset_media_jobs_item_status",
            "dataset_item_id",
            "status",
        ),
    )


class AssetMediaOperation(Base):
    __tablename__ = "asset_media_operations"

    operation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True
    )
    actor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    scope: Mapped[dict] = mapped_column(JSONB, nullable=False)
    intent: Mapped[dict] = mapped_column(JSONB, nullable=False)
    result: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "action IN ('request', 'retry', 'cancel')",
            name="ck_asset_media_operations_action",
        ),
        CheckConstraint(
            "pg_catalog.jsonb_typeof(scope) = 'object'",
            name="ck_asset_media_operations_scope_object",
        ),
        CheckConstraint(
            "pg_catalog.jsonb_typeof(intent) = 'object'",
            name="ck_asset_media_operations_intent_object",
        ),
        CheckConstraint(
            "pg_catalog.jsonb_typeof(result) = 'object'",
            name="ck_asset_media_operations_result_object",
        ),
        Index("ix_asset_media_operations_created_at", "created_at"),
    )


class AssetMediaTimetableStaging(Base):
    __tablename__ = "asset_media_timetable_staging"

    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("asset_media_jobs.job_id", ondelete="CASCADE"),
        primary_key=True,
    )
    generation: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    frame_index: Mapped[int] = mapped_column(Integer, primary_key=True)
    lease_token: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    pts_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    is_keyframe: Mapped[bool] = mapped_column(Boolean, nullable=False)
    pict_type: Mapped[str | None] = mapped_column(String(8))
    byte_offset: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("generation >= 1", name="ck_asset_media_staging_generation"),
        CheckConstraint("frame_index >= 0", name="ck_asset_media_staging_frame"),
    )


class AssetMediaGarbage(Base):
    __tablename__ = "asset_media_garbage"

    garbage_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    prefix: Mapped[str | None] = mapped_column(String(1024))
    keys: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    reason: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    not_before: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    lease_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_ack_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))

    __table_args__ = (
        CheckConstraint(
            "(prefix IS NOT NULL AND pg_catalog.jsonb_typeof(keys) = 'array' "
            "AND pg_catalog.jsonb_array_length(keys) = 0) OR "
            "(prefix IS NULL AND pg_catalog.jsonb_typeof(keys) = 'array' "
            "AND pg_catalog.jsonb_array_length(keys) BETWEEN 1 AND 500)",
            name="ck_asset_media_garbage_scope",
        ),
        CheckConstraint(
            "(lease_token IS NULL) = (lease_expires_at IS NULL)",
            name="ck_asset_media_garbage_lease_pair",
        ),
        CheckConstraint(
            "attempt_count >= 0", name="ck_asset_media_garbage_attempt_count"
        ),
        CheckConstraint(
            "reason IN ('source_changed', 'dependency_changed', 'cache_expired', "
            "'missing_cache', 'attempt_abandoned')",
            name="ck_asset_media_garbage_reason",
        ),
        Index(
            "ix_asset_media_garbage_pending",
            "not_before",
            "created_at",
            "garbage_id",
            "lease_expires_at",
            postgresql_where=text("completed_at IS NULL"),
        ),
        Index(
            "uq_asset_media_garbage_attempt_prefix",
            "prefix",
            unique=True,
            postgresql_where=text("prefix LIKE 'media-assets/%'"),
        ),
    )
