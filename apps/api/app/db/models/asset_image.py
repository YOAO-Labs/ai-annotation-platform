"""Assets-owned image execution, immutable receipts and retained garbage."""

from datetime import datetime
import uuid

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
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


class AssetImageJob(Base):
    __tablename__ = "asset_image_jobs"

    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    owner_kind: Mapped[str | None] = mapped_column(String(16))
    owner_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    slot_key: Mapped[str] = mapped_column(String(255), nullable=False)
    generation: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    spec: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    source_generation: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )
    source_snapshot: Mapped[str | None] = mapped_column(String(64))
    source_facts: Mapped[dict | None] = mapped_column(JSONB)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    progress: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    result: Mapped[dict | None] = mapped_column(JSONB)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_detail: Mapped[str | None] = mapped_column(Text)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_owner: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    lease_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    output_prefix: Mapped[str | None] = mapped_column(String(1024))
    pyramid_asset_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    pyramid_generation: Mapped[int | None] = mapped_column(Integer)
    last_lease_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    last_outcome: Mapped[str | None] = mapped_column(String(24))
    accepted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    backfill_cutoff: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    backfill_cursor_kind: Mapped[str | None] = mapped_column(String(16))
    backfill_cursor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))

    __table_args__ = (
        CheckConstraint(
            "kind IN ('dimensions', 'thumbnail', 'pyramid', 'image_backfill')",
            name="ck_asset_image_jobs_kind",
        ),
        CheckConstraint(
            "status IN ('pending', 'running', 'ready', 'failed', 'canceled')",
            name="ck_asset_image_jobs_status",
        ),
        CheckConstraint("generation >= 1", name="ck_asset_image_jobs_generation"),
        CheckConstraint(
            "source_generation >= 0", name="ck_asset_image_jobs_source_generation"
        ),
        CheckConstraint("attempt_count >= 0", name="ck_asset_image_jobs_attempt_count"),
        CheckConstraint(
            "(lease_token IS NULL) = (lease_expires_at IS NULL) "
            "AND (lease_token IS NULL) = (lease_owner IS NULL)",
            name="ck_asset_image_jobs_lease",
        ),
        CheckConstraint(
            "(last_lease_token IS NULL) = (last_outcome IS NULL)",
            name="ck_asset_image_jobs_last_outcome",
        ),
        CheckConstraint(
            "(kind = 'image_backfill' AND owner_kind IS NULL AND owner_id IS NULL "
            "AND source_generation = 0 AND source_snapshot IS NULL "
            "AND source_facts IS NULL AND backfill_cutoff IS NOT NULL) OR "
            "(kind <> 'image_backfill' AND owner_kind IN ('dataset_item', 'task') "
            "AND owner_id IS NOT NULL AND source_generation > 0 "
            "AND source_snapshot IS NOT NULL AND source_facts IS NOT NULL "
            "AND backfill_cutoff IS NULL AND backfill_cursor_kind IS NULL "
            "AND backfill_cursor_id IS NULL)",
            name="ck_asset_image_jobs_scope",
        ),
        CheckConstraint(
            "(backfill_cursor_kind IS NULL) = (backfill_cursor_id IS NULL)",
            name="ck_asset_image_jobs_cursor",
        ),
        Index(
            "uq_asset_image_jobs_owner_slot",
            "owner_kind",
            "owner_id",
            "slot_key",
            unique=True,
            postgresql_where=text("owner_id IS NOT NULL"),
        ),
        Index(
            "uq_asset_image_jobs_backfill_slot",
            "slot_key",
            unique=True,
            postgresql_where=text("owner_id IS NULL"),
        ),
        Index("ix_asset_image_jobs_claim", "status", "lease_expires_at", "accepted_at"),
        Index("ix_asset_image_jobs_owner_status", "owner_kind", "owner_id", "status"),
    )


class AssetImageOperation(Base):
    __tablename__ = "asset_image_operations"

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
            name="ck_asset_image_operations_action",
        ),
        Index("ix_asset_image_operations_created_at", "created_at"),
    )


class AssetImageGarbage(Base):
    __tablename__ = "asset_image_garbage"

    garbage_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    prefix: Mapped[str] = mapped_column(String(1024), nullable=False, unique=True)
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    not_before: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_owner: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    lease_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_ack_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))

    __table_args__ = (
        CheckConstraint(
            "(lease_token IS NULL) = (lease_expires_at IS NULL) "
            "AND (lease_token IS NULL) = (lease_owner IS NULL)",
            name="ck_asset_image_garbage_lease",
        ),
        Index(
            "ix_asset_image_garbage_due", "not_before", "lease_expires_at", "garbage_id"
        ),
    )
