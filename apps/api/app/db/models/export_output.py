"""Independent physical ownership survives export job and cache retention."""

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
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

EXPORT_STORAGE_REF_CHECK = """
COALESCE((
    jsonb_typeof(storage_ref) = 'object'
    AND storage_ref ?& ARRAY['schema','purpose','location_id','config_revision',
        'binding_generation','endpoint_url','bucket','object_key','storage_identity']
    AND storage_ref - ARRAY['schema','purpose','location_id','config_revision',
        'binding_generation','endpoint_url','bucket','object_key','storage_identity']::text[] = '{}'::jsonb
    AND storage_ref->'schema' = '1'::jsonb AND storage_ref->>'schema' = '1'
    AND storage_ref->>'purpose' = 'export'
    AND storage_ref->>'location_id' = '4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7'
    AND storage_ref->'config_revision' = '1'::jsonb AND storage_ref->>'config_revision' = '1'
    AND storage_ref->'binding_generation' = '1'::jsonb AND storage_ref->>'binding_generation' = '1'
    AND jsonb_typeof(storage_ref->'endpoint_url') = 'string'
    AND storage_ref->>'endpoint_url' ~ '^https?://[^[:space:]/?#@]+$'
    AND position(chr(92) in (storage_ref->>'endpoint_url')) = 0
    AND jsonb_typeof(storage_ref->'bucket') = 'string'
    AND length(storage_ref->>'bucket') BETWEEN 1 AND 255
    AND storage_ref->>'bucket' !~ '[[:space:]/?#@]'
    AND position(chr(92) in (storage_ref->>'bucket')) = 0
    AND storage_ref->>'object_key' = media || '/' || project_id::text || '/'
        || job_id::text || '/' || id::text || '.zip'
    AND jsonb_typeof(storage_ref->'storage_identity') = 'string'
    AND storage_ref->>'storage_identity' ~ '^[0-9a-f]{64}$'
), false)
"""


class ExportOutput(Base):
    __tablename__ = "export_outputs"
    __table_args__ = (
        CheckConstraint("media IN ('image','video')", name="ck_export_output_media"),
        CheckConstraint(
            "length(format) BETWEEN 1 AND 1000 AND format !~ '[[:cntrl:]]'",
            name="ck_export_output_format",
        ),
        CheckConstraint("file_count >= 0", name="ck_export_output_file_count"),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_export_output_sha256"),
        CheckConstraint(
            "size_bytes > 0 AND size_bytes <= 68719476736", name="ck_export_output_size"
        ),
        CheckConstraint(
            "state IN ('pending','ready','retired')", name="ck_export_output_state"
        ),
        CheckConstraint(
            "expires_at > created_at AND expires_at <= created_at + INTERVAL '168 hours'",
            name="ck_export_output_expiry",
        ),
        CheckConstraint(
            "(state='retired' AND retired_at IS NOT NULL) OR (state<>'retired' AND retired_at IS NULL)",
            name="ck_export_output_retirement",
        ),
        CheckConstraint(EXPORT_STORAGE_REF_CHECK, name="ck_export_output_storage_ref"),
        Index("ix_export_output_cleanup", "state", "last_attempted_at", "id"),
        Index("ix_export_output_job", "job_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # Snapshots have no FK: cache, project or job deletion must retain cleanup facts.
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    job_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    media: Mapped[str] = mapped_column(String(5), nullable=False)
    format: Mapped[str] = mapped_column(Text, nullable=False)
    file_count: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    storage_ref: Mapped[dict] = mapped_column(JSONB(none_as_null=True), nullable=False)
    state: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
