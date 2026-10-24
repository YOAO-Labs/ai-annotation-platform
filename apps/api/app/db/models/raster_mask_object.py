"""Independent RLE candidates and durable guards for business publication."""

from datetime import datetime
import uuid

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

RASTER_OBJECT_LOGICAL_KEY_CHECK = """
logical_key = 'raster-masks/sha256/' || substr(canonical_sha256,1,2) || '/'
    || substr(canonical_sha256,3,2) || '/' || canonical_sha256
    || CASE WHEN storage_encoding='gzip' THEN '.json.gz' ELSE '.json' END
"""
RASTER_OBJECT_STORAGE_REF_CHECK = """
COALESCE((
    jsonb_typeof(storage_ref) = 'object'
    AND storage_ref ?& ARRAY['schema','purpose','location_id','config_revision',
        'binding_generation','endpoint_url','bucket','object_key','storage_identity']
    AND storage_ref - ARRAY['schema','purpose','location_id','config_revision',
        'binding_generation','endpoint_url','bucket','object_key','storage_identity']::text[] = '{}'::jsonb
    AND storage_ref->'schema' = '1'::jsonb AND storage_ref->>'schema' = '1'
    AND storage_ref->>'purpose' = 'annotations'
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
    AND storage_ref->>'object_key' = 'raster-masks/objects/' || id::text
        || CASE WHEN storage_encoding='gzip' THEN '.json.gz' ELSE '.json' END
    AND jsonb_typeof(storage_ref->'storage_identity') = 'string'
    AND storage_ref->>'storage_identity' ~ '^[0-9a-f]{64}$'
), false)
"""
RASTER_OBJECT_PUBLICATION_CHECK = """
(state='pending' AND published_at IS NULL)
OR (state='ready' AND published_at IS NOT NULL AND published_at >= created_at)
OR (state='retired' AND (published_at IS NULL OR published_at >= created_at))
"""
RASTER_OBJECT_LAST_USED_CHECK = """
(published_at IS NULL AND last_used_at IS NULL)
OR (state <> 'pending' AND published_at IS NOT NULL AND last_used_at IS NOT NULL
    AND last_used_at >= published_at)
"""
RASTER_OBJECT_RETIREMENT_CHECK = """
(state='retired' AND retired_at IS NOT NULL
    AND retired_at >= COALESCE(last_used_at,published_at,created_at))
OR (state<>'retired' AND retired_at IS NULL)
"""
RASTER_OBJECT_IDENTITY_CHECK = """
(storage_encoding='identity' AND stored_sha256=canonical_sha256
    AND stored_size_bytes=canonical_size_bytes)
OR (storage_encoding='gzip' AND canonical_size_bytes <= stored_size_bytes::bigint * 20)
"""


class RasterMaskObject(Base):
    __tablename__ = "raster_mask_objects"
    __table_args__ = (
        CheckConstraint(
            "canonical_sha256 ~ '^[0-9a-f]{64}$'", name="ck_raster_object_canonical_sha"
        ),
        CheckConstraint(
            "stored_sha256 ~ '^[0-9a-f]{64}$'", name="ck_raster_object_stored_sha"
        ),
        CheckConstraint(
            "canonical_size_bytes BETWEEN 1 AND 4194304",
            name="ck_raster_object_canonical_size",
        ),
        CheckConstraint(
            "stored_size_bytes BETWEEN 1 AND 8388608",
            name="ck_raster_object_stored_size",
        ),
        CheckConstraint(
            "height BETWEEN 1 AND 8192 AND width BETWEEN 1 AND 8192 AND height::bigint * width <= 67108864",
            name="ck_raster_object_dimensions",
        ),
        CheckConstraint("runs BETWEEN 1 AND 1000000", name="ck_raster_object_runs"),
        CheckConstraint(
            "storage_encoding IN ('identity','gzip')", name="ck_raster_object_encoding"
        ),
        CheckConstraint(
            RASTER_OBJECT_LOGICAL_KEY_CHECK, name="ck_raster_object_logical_key"
        ),
        CheckConstraint(
            RASTER_OBJECT_IDENTITY_CHECK, name="ck_raster_object_identity_bytes"
        ),
        CheckConstraint(
            RASTER_OBJECT_STORAGE_REF_CHECK, name="ck_raster_object_storage_ref"
        ),
        CheckConstraint(
            "state IN ('pending','ready','retired')", name="ck_raster_object_state"
        ),
        CheckConstraint(
            RASTER_OBJECT_PUBLICATION_CHECK, name="ck_raster_object_publication"
        ),
        CheckConstraint(
            RASTER_OBJECT_LAST_USED_CHECK, name="ck_raster_object_last_used"
        ),
        CheckConstraint(
            RASTER_OBJECT_RETIREMENT_CHECK, name="ck_raster_object_retirement"
        ),
        Index(
            "uq_raster_object_ready_logical_key",
            "logical_key",
            unique=True,
            postgresql_where=text("state='ready'"),
        ),
        Index("ix_raster_object_cleanup", "state", "last_attempted_at", "id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    logical_key: Mapped[str] = mapped_column(String(255), nullable=False)
    canonical_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    canonical_size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    height: Mapped[int] = mapped_column(Integer, nullable=False)
    width: Mapped[int] = mapped_column(Integer, nullable=False)
    runs: Mapped[int] = mapped_column(Integer, nullable=False)
    storage_encoding: Mapped[str] = mapped_column(String(8), nullable=False)
    stored_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    stored_size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    storage_ref: Mapped[dict] = mapped_column(JSONB(none_as_null=True), nullable=False)
    state: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RasterMaskPublication(Base):
    __tablename__ = "raster_mask_publications"
    __table_args__ = (
        CheckConstraint(
            "gate_revision > 0", name="ck_raster_publication_gate_revision"
        ),
        Index("ix_raster_publication_object", "object_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    object_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("raster_mask_objects.id", ondelete="RESTRICT"),
        nullable=False,
    )
    gate_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
