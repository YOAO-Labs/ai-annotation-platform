"""Current standalone-task locator, separate from immutable creation evidence."""

from datetime import datetime
import uuid

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models.annotations_storage_ref import annotations_ref_check


TASK_ORIGINAL_STORAGE_REF_CHECK = (
    "storage_ref IS NULL OR ("
    + annotations_ref_check(
        "'__aap_task_originals__/' || task_id::text || '/' || id::text || '/payload'"
    )
    + " AND storage_ref->>'location_id'=location_id::text AND storage_ref->>'bucket'=bucket AND storage_ref->>'object_key'=object_key AND storage_ref->>'storage_identity'=storage_identity)"
)


class TaskOriginal(Base):
    __tablename__ = "task_originals"
    __table_args__ = (
        CheckConstraint(
            TASK_ORIGINAL_STORAGE_REF_CHECK, name="ck_task_original_storage_ref"
        ),
        UniqueConstraint("creation_id", name="uq_task_original_creation"),
        CheckConstraint("id = creation_id", name="ck_task_original_creation_id"),
        CheckConstraint(
            "storage_ref IS NOT NULL OR location_id = '4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7'::uuid",
            name="ck_task_original_location",
        ),
        CheckConstraint(
            "sha256 ~ '^[0-9a-f]{64}$' AND storage_identity ~ '^[0-9a-f]{64}$'",
            name="ck_task_original_sha",
        ),
        CheckConstraint("size_bytes >= 0", name="ck_task_original_size"),
        CheckConstraint(
            "object_key = COALESCE(storage_ref->>'key_prefix','') || '__aap_task_originals__/' || task_id::text || '/' || id::text || '/payload'",
            name="ck_task_original_key",
        ),
        CheckConstraint(
            "length(bucket) BETWEEN 1 AND 255 AND bucket !~ '[[:space:][:cntrl:]/?#@]' AND position(chr(92) in bucket) = 0",
            name="ck_task_original_bucket",
        ),
        CheckConstraint(
            "length(etag) BETWEEN 1 AND 1024 AND etag !~ '[[:cntrl:]]'",
            name="ck_task_original_etag",
        ),
        CheckConstraint(
            "version_id IS NULL OR (length(version_id) BETWEEN 1 AND 1024 AND version_id <> 'null' AND version_id !~ '[[:cntrl:]]')",
            name="ck_task_original_version",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    creation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("managed_original_creations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    # Business snapshots deliberately have no FK or cascade.
    task_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    location_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    storage_identity: Mapped[str] = mapped_column(String(64), nullable=False)
    bucket: Mapped[str] = mapped_column(String(255), nullable=False)
    object_key: Mapped[str] = mapped_column(String(1000), nullable=False)
    storage_ref: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # These are source write receipts, not target HEAD conditions after restore.
    etag: Mapped[str] = mapped_column(Text, nullable=False)
    version_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
