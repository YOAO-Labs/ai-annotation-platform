"""Independent video-derived media attempts and their saved physical ownership."""

from datetime import datetime
import uuid

from sqlalchemy import CheckConstraint, DateTime, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


VIDEO_DERIVATIVE_STORAGE_REF_CHECK = r"""
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
    AND storage_ref->>'prefix' = 'video-derivatives/objects/' || id::text || '/'
    AND jsonb_typeof(storage_ref->'storage_identity') = 'string'
    AND storage_ref->>'storage_identity' ~ '^[0-9a-f]{64}$'
), false)
"""
VIDEO_DERIVATIVE_INPUT_SHAPE_CHECK = """
COALESCE(jsonb_typeof(spec) = 'object'
    AND jsonb_typeof(original_ref) = 'object'
    AND jsonb_typeof(auth_ref) = 'object', false)
"""
VIDEO_DERIVATIVE_PUBLICATION_CHECK = """
COALESCE((state != 'ready' OR (inventory IS NOT NULL AND result IS NOT NULL))
    AND (state != 'building' OR (inventory IS NULL AND result IS NULL))
    AND ((inventory IS NULL AND result IS NULL)
        OR (jsonb_typeof(inventory) = 'object'
            AND inventory ?& ARRAY['schema','objects']
            AND inventory - ARRAY['schema','objects']::text[] = '{}'::jsonb
            AND inventory->'schema' = '1'::jsonb AND inventory->>'schema' = '1'
            AND jsonb_typeof(inventory->'objects') = 'object'
            AND jsonb_typeof(result) = 'object')), false)
"""
VIDEO_DERIVATIVE_LEASE_CHECK = """
((lease_token IS NULL) = (lease_expires_at IS NULL))
    AND (state != 'ready' OR lease_token IS NOT NULL)
"""


class VideoDerivativeOutput(Base):
    """One immutable physical attempt that survives business-object cascades."""

    __tablename__ = "video_derivative_outputs"
    __mapper_args__ = {"eager_defaults": True}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    dataset_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    spec: Mapped[dict] = mapped_column(JSONB(none_as_null=True), nullable=False)
    original_ref: Mapped[dict] = mapped_column(JSONB(none_as_null=True), nullable=False)
    auth_ref: Mapped[dict] = mapped_column(JSONB(none_as_null=True), nullable=False)
    metadata_output_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    storage_ref: Mapped[dict] = mapped_column(JSONB(none_as_null=True), nullable=False)
    state: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="building"
    )
    lease_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    inventory: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    result: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    error_code: Mapped[str | None] = mapped_column(String(128))
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
            "kind IN ('metadata', 'frame', 'chunk')", name="ck_video_derivative_kind"
        ),
        CheckConstraint(
            "state IN ('building', 'ready', 'retiring', 'deleted')",
            name="ck_video_derivative_state",
        ),
        CheckConstraint(
            "(kind = 'metadata') = (metadata_output_id IS NULL)",
            name="ck_video_derivative_metadata",
        ),
        CheckConstraint(
            VIDEO_DERIVATIVE_INPUT_SHAPE_CHECK, name="ck_video_derivative_input_shape"
        ),
        CheckConstraint(VIDEO_DERIVATIVE_LEASE_CHECK, name="ck_video_derivative_lease"),
        CheckConstraint(
            VIDEO_DERIVATIVE_STORAGE_REF_CHECK,
            name="ck_video_derivative_storage_ref",
        ),
        CheckConstraint(
            VIDEO_DERIVATIVE_PUBLICATION_CHECK,
            name="ck_video_derivative_publication",
        ),
        Index("ix_video_derivative_outputs_cleanup", "state", "updated_at", "id"),
        Index("ix_video_derivative_outputs_item_kind", "dataset_item_id", "kind", "id"),
    )
