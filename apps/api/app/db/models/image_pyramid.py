from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.thumbnail_output import media_cache_ref_check
from app.db.base import Base


_OUTPUT_REF_CHECK = r"""
COALESCE((
    jsonb_typeof(storage_ref) = 'object'
    AND storage_ref ?& ARRAY['schema','purpose','location_id','config_revision',
        'binding_generation','endpoint_url','bucket','prefix','storage_identity']
    AND storage_ref - ARRAY['schema','purpose','location_id','config_revision',
        'binding_generation','endpoint_url','bucket','prefix','storage_identity']::text[] = '{}'::jsonb
    AND storage_ref->'schema' = '1'::jsonb AND storage_ref->>'schema' = '1'
    AND storage_ref->>'purpose' = 'media-cache'
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
    AND storage_ref->>'prefix' = 'image-pyramids/objects/' || id::text || '/'
    AND jsonb_typeof(storage_ref->'storage_identity') = 'string'
    AND storage_ref->>'storage_identity' ~ '^[0-9a-f]{64}$'
), false)
"""


_OUTPUT_REF_CHECK = (
    "("
    + _OUTPUT_REF_CHECK
    + ") OR ("
    + media_cache_ref_check("'image-pyramids/objects/' || id::text || '/'")
    + ")"
)


class ImagePyramidAsset(Base):
    """Stable owner/profile identity for an immutable image-pyramid lineage."""

    __tablename__ = "image_pyramid_assets"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    dataset_item_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dataset_items.id", ondelete="CASCADE"),
        nullable=True,
    )
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=True,
    )
    profile_version: Mapped[str] = mapped_column(String(64), nullable=False)
    active_generation: Mapped[int | None] = mapped_column(Integer, nullable=True)
    building_generation: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        CheckConstraint(
            "(dataset_item_id IS NOT NULL) <> (task_id IS NOT NULL)",
            name="ck_image_pyramid_assets_owner_xor",
        ),
        Index(
            "uq_image_pyramid_assets_dataset_profile",
            "dataset_item_id",
            "profile_version",
            unique=True,
            postgresql_where=text("dataset_item_id IS NOT NULL"),
        ),
        Index(
            "uq_image_pyramid_assets_task_profile",
            "task_id",
            "profile_version",
            unique=True,
            postgresql_where=text("task_id IS NOT NULL"),
        ),
    )


class ImagePyramidGeneration(Base):
    """One immutable attempt beneath an :class:`ImagePyramidAsset`."""

    __tablename__ = "image_pyramid_generations"

    asset_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("image_pyramid_assets.id", ondelete="CASCADE"),
        nullable=False,
    )
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    original_ref: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    auth_ref: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    output_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("image_pyramid_outputs.id", ondelete="RESTRICT")
    )
    source_identity: Mapped[str] = mapped_column(String(255), nullable=False)
    source_fingerprint: Mapped[str | None] = mapped_column(String(80), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    lease_token: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tile_size: Mapped[int] = mapped_column(
        Integer, nullable=False, default=512, server_default="512"
    )
    overlap: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    max_level: Mapped[int | None] = mapped_column(Integer, nullable=True)
    format: Mapped[str | None] = mapped_column(String(16), nullable=True)
    normalization_version: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    manifest_digest: Mapped[str | None] = mapped_column(String(80), nullable=True)
    overview_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    overview_width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    overview_height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    overview_digest: Mapped[str | None] = mapped_column(String(80), nullable=True)
    tile_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    retained_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    last_accessed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        PrimaryKeyConstraint(
            "asset_id", "generation", name="pk_image_pyramid_generations"
        ),
        CheckConstraint("generation > 0", name="ck_image_pyramid_generation_positive"),
        CheckConstraint(
            "status IN ('pending', 'building', 'ready', 'failed')",
            name="ck_image_pyramid_generation_status",
        ),
        CheckConstraint("tile_size > 0", name="ck_image_pyramid_tile_size_positive"),
        CheckConstraint("overlap >= 0", name="ck_image_pyramid_overlap_nonnegative"),
        Index(
            "uq_image_pyramid_generation_inflight",
            "asset_id",
            unique=True,
            postgresql_where=text("status IN ('pending', 'building')"),
        ),
        CheckConstraint(
            "COALESCE((original_ref IS NULL AND auth_ref IS NULL) OR "
            "(jsonb_typeof(original_ref) = 'object' AND jsonb_typeof(auth_ref) = 'object'), false)",
            name="ck_image_pyramid_input_receipts",
        ),
        Index(
            "ix_image_pyramid_generations_status_updated",
            "status",
            "updated_at",
        ),
    )


class ImagePyramidOutput(Base):
    """One saved physical attempt, retained independently of its business owner."""

    __tablename__ = "image_pyramid_outputs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    asset_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    lease_token: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    storage_ref: Mapped[dict] = mapped_column(JSONB(none_as_null=True), nullable=False)
    state: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="building"
    )
    inventory: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        CheckConstraint("generation > 0", name="ck_image_pyramid_output_generation"),
        CheckConstraint(
            "state IN ('building', 'ready', 'retiring', 'deleted')",
            name="ck_image_pyramid_output_state",
        ),
        CheckConstraint(
            "(state != 'ready' OR inventory IS NOT NULL) "
            "AND (state != 'building' OR inventory IS NULL) "
            "AND (inventory IS NULL OR COALESCE((jsonb_typeof(inventory) = 'object' "
            "AND inventory ?& ARRAY['schema','objects'] "
            "AND inventory - ARRAY['schema','objects']::text[] = '{}'::jsonb "
            "AND inventory->'schema' = '1'::jsonb AND inventory->>'schema' = '1' "
            "AND jsonb_typeof(inventory->'objects') = 'object' "
            "AND inventory->'objects' ? (storage_ref->>'prefix' || 'manifest.json') "
            "AND inventory->'objects' ? (storage_ref->>'prefix' || 'overview.webp')), false))",
            name="ck_image_pyramid_output_inventory",
        ),
        CheckConstraint(_OUTPUT_REF_CHECK, name="ck_image_pyramid_output_storage_ref"),
        Index("ix_image_pyramid_outputs_cleanup", "state", "updated_at", "id"),
    )
