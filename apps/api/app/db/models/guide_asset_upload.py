"""Independent guide-image ownership survives the original project and account."""

from datetime import datetime
import uuid

from sqlalchemy import CheckConstraint, DateTime, Index, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models.annotations_storage_ref import annotations_ref_check

LEGACY_GUIDE_ASSET_STORAGE_REF_CHECK = """
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
    AND storage_ref->>'object_key' = 'projects/' || project_id::text || '/guide/' || id::text
    AND jsonb_typeof(storage_ref->'storage_identity') = 'string'
    AND storage_ref->>'storage_identity' ~ '^[0-9a-f]{64}$'
), false)
"""
GUIDE_ASSET_STORAGE_REF_CHECK = (
    "("
    + LEGACY_GUIDE_ASSET_STORAGE_REF_CHECK
    + ") OR ("
    + annotations_ref_check("'projects/' || project_id::text || '/guide/' || id::text")
    + ")"
)

GUIDE_ASSET_FILE_NAME_CHECK = """
length(file_name) BETWEEN 1 AND 255
AND file_name NOT IN ('.','..')
AND position('/' in file_name) = 0
AND position(chr(92) in file_name) = 0
AND file_name !~ '[[:cntrl:]]'
AND file_name !~ ('[' || chr(127) || '-' || chr(159) || ']')
"""
GUIDE_ASSET_MIME_CHECK = """
mime_type IN ('image/png','image/jpeg','image/webp','image/gif','image/svg+xml')
"""


class GuideAssetUpload(Base):
    __tablename__ = "guide_asset_uploads"
    __table_args__ = (
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_guide_asset_sha256"),
        CheckConstraint("size_bytes BETWEEN 1 AND 5242880", name="ck_guide_asset_size"),
        CheckConstraint(GUIDE_ASSET_MIME_CHECK, name="ck_guide_asset_mime_type"),
        CheckConstraint(GUIDE_ASSET_FILE_NAME_CHECK, name="ck_guide_asset_file_name"),
        CheckConstraint(
            "state IN ('pending','ready','retired')", name="ck_guide_asset_state"
        ),
        CheckConstraint(
            "(state='retired' AND retired_at IS NOT NULL AND retired_at >= created_at) OR (state<>'retired' AND retired_at IS NULL)",
            name="ck_guide_asset_retirement",
        ),
        CheckConstraint(
            GUIDE_ASSET_STORAGE_REF_CHECK,
            name="ck_guide_asset_storage_ref",
        ),
        Index("ix_guide_asset_cleanup", "state", "last_attempted_at", "id"),
        Index("ix_guide_asset_original_project", "project_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # Historical snapshots have no FK: business deletion cannot erase responsibility.
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    storage_ref: Mapped[dict] = mapped_column(JSONB(none_as_null=True), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False)
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    state: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
