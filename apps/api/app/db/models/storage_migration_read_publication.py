"""Immutable exact-version read routes retain their sealed migration owner."""

from datetime import datetime
import uuid

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class StorageMigrationReadPublication(Base):
    __tablename__ = "storage_migration_read_publications"
    __table_args__ = (
        CheckConstraint(
            "state IN ('active','withdrawing','withdrawn')",
            name="ck_migration_read_publication_state",
        ),
        CheckConstraint(
            "revision >= 1 AND finalization_revision >= 1 AND window_revision >= 1",
            name="ck_migration_read_publication_revision",
        ),
        CheckConstraint(
            "mapping_sha256 ~ '^[a-f0-9]{64}$' AND route_count BETWEEN 0 AND 100000",
            name="ck_migration_read_publication_mapping",
        ),
        CheckConstraint(
            "octet_length(source::text) <= 65536 AND octet_length(target::text) <= 65536 "
            "AND octet_length(token::text) <= 65536 AND octet_length(evidence::text) <= 65536 "
            "AND octet_length(native::text) <= 65536",
            name="ck_migration_read_publication_budget",
        ),
        CheckConstraint(
            "(state = 'withdrawn') = (withdrawn_at IS NOT NULL) "
            "AND (state <> 'active') = (withdrawing_at IS NOT NULL)",
            name="ck_migration_read_publication_withdrawn",
        ),
        Index(
            "uq_migration_read_publication_active_source",
            "source_location_id",
            unique=True,
            postgresql_where=text("state <> 'withdrawn'"),
        ),
        Index("ix_migration_read_publication_copy", "copy_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("storage_migration_finalizations.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    window_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("storage_migration_windows.id", ondelete="RESTRICT")
    )
    copy_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("storage_migration_copies.id", ondelete="RESTRICT")
    )
    source_location_id: Mapped[uuid.UUID]
    target_location_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("storage_locations.id", ondelete="RESTRICT")
    )
    state: Mapped[str] = mapped_column(String(16), default="active")
    revision: Mapped[int] = mapped_column(default=1)
    finalization_revision: Mapped[int]
    window_revision: Mapped[int]
    source: Mapped[dict] = mapped_column(JSONB)
    target: Mapped[dict] = mapped_column(JSONB)
    target_revisions: Mapped[dict] = mapped_column(JSONB)
    token: Mapped[dict] = mapped_column(JSONB)
    evidence: Mapped[dict] = mapped_column(JSONB)
    native: Mapped[dict] = mapped_column(JSONB)
    mapping_sha256: Mapped[str] = mapped_column(String(64))
    route_count: Mapped[int]
    created_by: Mapped[uuid.UUID]
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    withdrawing_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    withdrawn_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class StorageMigrationReadRoute(Base):
    __tablename__ = "storage_migration_read_routes"
    __table_args__ = (
        CheckConstraint(
            "source_object_id ~ '^[a-f0-9]{64}$' AND sha256 ~ '^[a-f0-9]{64}$' "
            "AND target_content_sha256 ~ '^[a-f0-9]{64}$' AND size_bytes >= 0",
            name="ck_migration_read_route_content",
        ),
        CheckConstraint(
            "target_version_id NOT IN ('','null') AND target_etag <> '' "
            "AND source_etag <> '' AND nonce <> '' AND creation_plan_uuid <> ''",
            name="ck_migration_read_route_receipt",
        ),
        CheckConstraint(
            "octet_length(endpoint_url) <= 2048 AND octet_length(bucket) BETWEEN 1 AND 255 "
            "AND octet_length(object_key) BETWEEN 1 AND 1024 "
            "AND octet_length(target_key) BETWEEN 1 AND 1024 "
            "AND octet_length(source_etag) <= 1024 AND octet_length(target_etag) <= 1024 "
            "AND octet_length(target_version_id) <= 1024 "
            "AND (source_version_id IS NULL OR octet_length(source_version_id) <= 1024)",
            name="ck_migration_read_route_budget",
        ),
        Index(
            "ix_migration_read_route_lookup",
            "publication_id",
            "sha256",
            "size_bytes",
        ),
    )

    publication_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("storage_migration_read_publications.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    source_object_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    endpoint_url: Mapped[str] = mapped_column(Text)
    bucket: Mapped[str] = mapped_column(Text)
    object_key: Mapped[str] = mapped_column(Text)
    source_version_id: Mapped[str | None] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    source_etag: Mapped[str] = mapped_column(Text)
    is_latest: Mapped[bool]
    target_key: Mapped[str] = mapped_column(Text)
    target_version_id: Mapped[str] = mapped_column(Text)
    target_etag: Mapped[str] = mapped_column(Text)
    target_content_sha256: Mapped[str] = mapped_column(String(64))
    nonce: Mapped[str] = mapped_column(Text)
    creation_plan_uuid: Mapped[str] = mapped_column(Text)
