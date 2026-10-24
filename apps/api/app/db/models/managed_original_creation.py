"""Durable controlled writes; creation evidence never grants exclusive authority."""

from datetime import datetime
import uuid

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ManagedOriginalCreation(Base):
    __tablename__ = "managed_original_creations"
    __table_args__ = (
        UniqueConstraint(
            "storage_identity",
            "bucket",
            "object_key",
            name="uq_managed_creation_reserved_key",
        ),
        Index("ix_managed_creation_dataset", "dataset_id", "allocated_at", "id"),
        Index("ix_managed_creation_task", "task_id", "allocated_at", "id"),
        CheckConstraint(
            "(owner_kind='dataset' AND dataset_id IS NOT NULL AND project_id IS NULL AND task_id IS NULL) OR (owner_kind='task' AND dataset_id IS NULL AND project_id IS NOT NULL AND task_id IS NOT NULL AND origin='direct' AND object_key='__aap_task_originals__/' || task_id::text || '/' || id::text || '/payload')",
            name="ck_managed_creation_scope",
        ),
        CheckConstraint(
            "origin IN ('direct','zip','connector','directory_copy')",
            name="ck_managed_creation_origin",
        ),
        CheckConstraint(
            "status IN ('allocated','preparing','prepared','writing','written','verified','failed','conflict','unknown')",
            name="ck_managed_creation_status",
        ),
        CheckConstraint(
            "max_bytes >= 0 AND timeout_seconds >= 60 AND maintenance_revision >= 1",
            name="ck_managed_creation_budget",
        ),
        CheckConstraint(
            "storage_identity ~ '^[0-9a-f]{64}$' AND input_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_managed_creation_identity",
        ),
        CheckConstraint(
            "(origin='direct' AND staging_bucket IS NOT NULL AND staging_key IS NOT NULL) OR (origin<>'direct' AND staging_bucket IS NULL AND staging_key IS NULL)",
            name="ck_managed_creation_staging",
        ),
        CheckConstraint(
            "(prepared IS NULL AND sha256 IS NULL AND md5 IS NULL AND size_bytes IS NULL AND frozen_at IS NULL) OR (prepared IS NOT NULL AND sha256 IS NOT NULL AND md5 IS NOT NULL AND size_bytes IS NOT NULL AND frozen_at IS NOT NULL AND sha256 ~ '^[0-9a-f]{64}$' AND md5 ~ '^[0-9a-f]{32}$' AND size_bytes >= 0 AND size_bytes <= max_bytes)",
            name="ck_managed_creation_frozen",
        ),
        CheckConstraint(
            "(receipt IS NULL AND etag IS NULL AND version_id IS NULL AND request_id IS NULL AND acknowledged_at IS NULL) OR (receipt IS NOT NULL AND etag IS NOT NULL AND length(etag)>0 AND acknowledged_at IS NOT NULL AND write_started_at IS NOT NULL AND prepared IS NOT NULL)",
            name="ck_managed_creation_receipt",
        ),
        CheckConstraint(
            "version_id IS NULL OR (length(version_id)>0 AND version_id<>'null')",
            name="ck_managed_creation_version",
        ),
        CheckConstraint(
            "error_code IS NULL OR error_code ~ '^[a-z][a-z0-9_]{0,79}$'",
            name="ck_managed_creation_error",
        ),
        CheckConstraint(
            "(verification IS NULL AND readback_at IS NULL) OR (verification IS NOT NULL AND readback_at IS NOT NULL AND receipt IS NOT NULL)",
            name="ck_managed_creation_readback",
        ),
        CheckConstraint(
            "(status IN ('failed','conflict','unknown') AND completed_at IS NOT NULL AND error_code IS NOT NULL) OR (status NOT IN ('failed','conflict','unknown') AND error_code IS NULL AND ((status='allocated' AND claimed_at IS NULL AND operation_id IS NULL AND prepared IS NULL AND completed_at IS NULL) OR (status='preparing' AND claimed_at IS NOT NULL AND prepared IS NULL AND completed_at IS NULL) OR (status='prepared' AND claimed_at IS NOT NULL AND operation_id IS NOT NULL AND prepared IS NOT NULL AND write_started_at IS NULL AND completed_at IS NULL) OR (status='writing' AND claimed_at IS NOT NULL AND operation_id IS NOT NULL AND prepared IS NOT NULL AND write_started_at IS NOT NULL AND receipt IS NULL AND completed_at IS NULL) OR (status='written' AND claimed_at IS NOT NULL AND operation_id IS NOT NULL AND receipt IS NOT NULL AND completed_at IS NULL) OR (status='verified' AND claimed_at IS NOT NULL AND operation_id IS NOT NULL AND receipt IS NOT NULL AND verification IS NOT NULL AND settled_at IS NOT NULL AND completed_at IS NOT NULL)))",
            name="ck_managed_creation_lifecycle",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    # No business/location/actor FK: independent persistence must never wait on
    # an uncommitted caller, or lose physical responsibility to a cascade.
    owner_kind: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="dataset"
    )
    dataset_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    project_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    task_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    location_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    storage_identity: Mapped[str] = mapped_column(String(64), nullable=False)
    bucket: Mapped[str] = mapped_column(String(255), nullable=False)
    object_key: Mapped[str] = mapped_column(String(1000), nullable=False)
    logical_path: Mapped[str] = mapped_column(String(1000), nullable=False)
    origin: Mapped[str] = mapped_column(String(20), nullable=False)
    max_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    timeout_seconds: Mapped[int] = mapped_column(nullable=False)
    content_type: Mapped[str] = mapped_column(String(255), nullable=False)
    requested_by: Mapped[uuid.UUID | None] = mapped_column()
    authority: Mapped[dict] = mapped_column(JSONB, nullable=False)
    configuration: Mapped[dict] = mapped_column(JSONB, nullable=False)
    input_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    maintenance_revision: Mapped[int] = mapped_column(nullable=False)
    staging_bucket: Mapped[str | None] = mapped_column(String(255))
    staging_key: Mapped[str | None] = mapped_column(String(1000), unique=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="allocated"
    )
    operation_id: Mapped[uuid.UUID | None] = mapped_column(unique=True)
    source: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    prepared: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    sha256: Mapped[str | None] = mapped_column(String(64))
    md5: Mapped[str | None] = mapped_column(String(32))
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    receipt: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    etag: Mapped[str | None] = mapped_column(Text)
    version_id: Mapped[str | None] = mapped_column(Text)
    request_id: Mapped[str | None] = mapped_column(Text)
    verification: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    error_code: Mapped[str | None] = mapped_column(String(80))
    allocated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    frozen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    write_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    readback_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
