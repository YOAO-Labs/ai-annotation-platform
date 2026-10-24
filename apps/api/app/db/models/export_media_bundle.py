"""Durable exact media delivery independently of business retention."""

from datetime import datetime
import uuid

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, column_property, mapped_column

from app.db.base import Base


EXPORT_MEDIA_STORAGE_REF_CHECK = r"""
COALESCE((
    jsonb_typeof(storage_ref) = 'object'
    AND storage_ref ?& ARRAY['schema','purpose','location_id','config_revision',
        'binding_generation','endpoint_url','bucket','key_prefix','storage_identity']
    AND storage_ref - ARRAY['schema','purpose','location_id','config_revision',
        'binding_generation','endpoint_url','bucket','key_prefix','storage_identity']::text[] = '{}'::jsonb
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
    AND storage_ref->>'key_prefix' = 'lidar-export-media/objects/' || id::text || '/'
    AND jsonb_typeof(storage_ref->'storage_identity') = 'string'
    AND storage_ref->>'storage_identity' ~ '^[0-9a-f]{64}$'
), false)
"""

EXPORT_MEDIA_DOCUMENTS_CHECK = """
COALESCE((jsonb_typeof(input_snapshot) = 'object'
    AND jsonb_typeof(logical_paths) = 'object'
    AND logical_paths ?& ARRAY['schema','targets']
    AND logical_paths - ARRAY['schema','targets']::text[] = '{}'::jsonb
    AND logical_paths->'schema' = '1'::jsonb AND logical_paths->>'schema' = '1'
    AND jsonb_typeof(logical_paths->'targets') = 'object'
    AND jsonb_typeof(inventory) = 'object'
    AND inventory ?& ARRAY['schema','objects']
    AND inventory - ARRAY['schema','objects']::text[] = '{}'::jsonb
    AND inventory->'schema' = '1'::jsonb AND inventory->>'schema' = '1'
    AND jsonb_typeof(inventory->'objects') = 'object'
    AND octet_length(input_snapshot::text) + octet_length(logical_paths::text)
        + octet_length(inventory::text) <= 268435456), false)
"""

EXPORT_MEDIA_VIDEO_CHECK = """
CASE WHEN input_snapshot->'schema' = '4'::jsonb THEN COALESCE((
    input_snapshot->>'schema' = '4'
    AND input_snapshot->'probe_contract' = '1'::jsonb
    AND input_snapshot->>'probe_contract' = '1'
    AND input_snapshot->'references' = '{}'::jsonb
    AND jsonb_typeof(input_snapshot->'project') = 'object'
    AND input_snapshot->'project'->>'id' = project_id::text
    AND input_snapshot->'project'->>'data_type' = 'video'
    AND jsonb_typeof(input_snapshot->'tasks') = 'array'
    AND jsonb_typeof(input_snapshot->'media') = 'array'
    AND jsonb_array_length(CASE WHEN jsonb_typeof(input_snapshot->'tasks') = 'array'
        THEN input_snapshot->'tasks' ELSE '[]'::jsonb END) <= 1000
    AND jsonb_array_length(CASE WHEN jsonb_typeof(input_snapshot->'media') = 'array'
        THEN input_snapshot->'media' ELSE '[]'::jsonb END)
        = jsonb_array_length(CASE WHEN jsonb_typeof(input_snapshot->'tasks') = 'array'
        THEN input_snapshot->'tasks' ELSE '[]'::jsonb END)
    AND object_count <= 1000 AND size_bytes <= 21474836480
), false) ELSE true END
"""


class ExportMediaBundle(Base):
    __tablename__ = "export_media_bundles"
    __table_args__ = (
        UniqueConstraint("zip_output_id", name="uq_export_media_bundle_zip_output"),
        CheckConstraint(
            "input_digest ~ '^[0-9a-f]{64}$'", name="ck_export_media_input_digest"
        ),
        CheckConstraint(EXPORT_MEDIA_DOCUMENTS_CHECK, name="ck_export_media_documents"),
        CheckConstraint(EXPORT_MEDIA_VIDEO_CHECK, name="ck_export_media_video"),
        CheckConstraint(
            "object_count BETWEEN 0 AND 21000 AND size_bytes BETWEEN 0 AND 25769803776",
            name="ck_export_media_budget",
        ),
        CheckConstraint(
            EXPORT_MEDIA_STORAGE_REF_CHECK, name="ck_export_media_storage_ref"
        ),
        CheckConstraint(
            "state IN ('building','ready','retiring','deleted')",
            name="ck_export_media_state",
        ),
        CheckConstraint(
            "expires_at = created_at + INTERVAL '168 hours'",
            name="ck_export_media_expiry",
        ),
        CheckConstraint(
            "((lease_token IS NULL) = (lease_expires_at IS NULL)) "
            "AND (lease_expires_at IS NULL OR lease_expires_at <= expires_at) "
            "AND (state != 'ready' OR lease_token IS NOT NULL)",
            name="ck_export_media_lease",
        ),
        CheckConstraint(
            "(state != 'building' OR published_at IS NULL) "
            "AND (state != 'ready' OR published_at IS NOT NULL) "
            "AND (published_at IS NULL OR "
            "(published_at >= created_at AND published_at < expires_at))",
            name="ck_export_media_publication",
        ),
        CheckConstraint(
            "((state IN ('retiring','deleted')) = (retired_at IS NOT NULL)) "
            "AND (retired_at IS NULL OR retired_at >= created_at)",
            name="ck_export_media_retirement",
        ),
        Index("ix_export_media_bundle_cleanup", "state", "last_attempted_at", "id"),
        Index("ix_export_media_bundle_job", "job_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # No business FKs: deletion must retain pending and published delivery facts.
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    job_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    zip_output_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    input_snapshot: Mapped[dict] = mapped_column(
        JSONB(none_as_null=True), nullable=False
    )
    # Select one authoritative scalar for physical/retirement fences without
    # loading the private frozen document during cleanup.
    input_schema = column_property(input_snapshot["schema"].label("input_schema"))
    input_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    logical_paths: Mapped[dict] = mapped_column(
        JSONB(none_as_null=True), nullable=False
    )
    inventory: Mapped[dict] = mapped_column(JSONB(none_as_null=True), nullable=False)
    object_count: Mapped[int] = mapped_column(Integer, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    storage_ref: Mapped[dict] = mapped_column(JSONB(none_as_null=True), nullable=False)
    state: Mapped[str] = mapped_column(
        String(16), nullable=False, default="building", server_default="building"
    )
    lease_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
