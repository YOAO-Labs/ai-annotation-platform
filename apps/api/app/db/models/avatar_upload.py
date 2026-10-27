"""Avatar objects retain physical ownership after their user reference changes."""

from datetime import datetime
import uuid

from sqlalchemy import CheckConstraint, DateTime, Index, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


# Keep the migration's copy independent from application imports.
AVATAR_STORAGE_REF_CHECK = """
(COALESCE((
    jsonb_typeof(storage_ref) = 'object'
    AND storage_ref ?& ARRAY['schema','purpose','location_id','config_revision',
        'binding_generation','endpoint_url','bucket','object_key','storage_identity']
    AND storage_ref - ARRAY['schema','purpose','location_id','config_revision',
        'binding_generation','endpoint_url','bucket','object_key','storage_identity']::text[] = '{}'::jsonb
    AND storage_ref->'schema' = '1'::jsonb AND storage_ref->>'schema' = '1'
    AND storage_ref->>'purpose' = 'avatars'
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
    AND storage_ref->>'object_key' = token || '.webp'
    AND jsonb_typeof(storage_ref->'storage_identity') = 'string'
    AND storage_ref->>'storage_identity' ~ '^[0-9a-f]{64}$'
), false)) OR (COALESCE((
    jsonb_typeof(storage_ref) = 'object'
    AND storage_ref ?& ARRAY['schema','purpose','location_id','config_revision',
        'binding_generation','endpoint_url','bucket','object_key','storage_identity']
    AND storage_ref - ARRAY['schema','purpose','location_id','config_revision',
        'binding_generation','endpoint_url','bucket','object_key','storage_identity']::text[] = '{}'::jsonb
    AND storage_ref->'schema' = '2'::jsonb AND storage_ref->>'schema' = '2'
    AND storage_ref->>'purpose' = 'avatars'
    AND jsonb_typeof(storage_ref->'location_id') = 'string'
    AND storage_ref->>'location_id' ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
    AND jsonb_typeof(storage_ref->'config_revision') = 'number'
    AND storage_ref->>'config_revision' ~ '^[1-9][0-9]*$'
    AND jsonb_typeof(storage_ref->'binding_generation') = 'number'
    AND storage_ref->>'binding_generation' ~ '^([2-9]|[1-9][0-9]+)$'
    AND jsonb_typeof(storage_ref->'endpoint_url') = 'string'
    AND storage_ref->>'endpoint_url' ~ '^https?://[^[:space:]/?#@]+$'
    AND position(chr(92) in (storage_ref->>'endpoint_url')) = 0
    AND jsonb_typeof(storage_ref->'bucket') = 'string'
    AND length(storage_ref->>'bucket') BETWEEN 1 AND 255
    AND storage_ref->>'bucket' !~ '[[:space:]/?#@]'
    AND position(chr(92) in (storage_ref->>'bucket')) = 0
    AND jsonb_typeof(storage_ref->'object_key') = 'string'
    AND octet_length(storage_ref->>'object_key') <= 1024
    AND storage_ref->>'object_key' !~ '[[:cntrl:]]'
    AND position(chr(92) in (storage_ref->>'object_key')) = 0
    AND position('//' in (storage_ref->>'object_key')) = 0
    AND left(storage_ref->>'object_key', 1) <> '/'
    AND storage_ref->>'object_key' !~ '(^|/)[.]{1,2}(/|$)'
    AND right(storage_ref->>'object_key', 37) = token || '.webp'
    AND (
        (storage_ref->>'location_id' = '4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7'
         AND storage_ref->>'config_revision' = '1'
         AND storage_ref->>'object_key' = token || '.webp')
        OR
        (storage_ref->>'location_id' <> '4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7'
         AND storage_ref->>'object_key' ~ '(^|/)_aap_managed/avatars/[0-9a-f]{32}[.]webp$')
    )
    AND jsonb_typeof(storage_ref->'storage_identity') = 'string'
    AND storage_ref->>'storage_identity' ~ '^[0-9a-f]{64}$'
), false))
"""


class AvatarUpload(Base):
    __tablename__ = "avatar_uploads"
    __table_args__ = (
        CheckConstraint("token ~ '^[0-9a-f]{32}$'", name="ck_avatar_upload_token"),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_avatar_upload_sha256"),
        CheckConstraint(
            "size_bytes > 0 AND size_bytes <= 2097152", name="ck_avatar_upload_size"
        ),
        CheckConstraint(
            "state IN ('pending','ready','retired')", name="ck_avatar_upload_state"
        ),
        CheckConstraint(
            "(state='retired' AND retired_at IS NOT NULL) OR "
            "(state<>'retired' AND retired_at IS NULL)",
            name="ck_avatar_upload_retirement",
        ),
        CheckConstraint(AVATAR_STORAGE_REF_CHECK, name="ck_avatar_upload_storage_ref"),
        CheckConstraint(
            "object_version_id IS NULL OR (length(object_version_id) BETWEEN 1 AND 1024 "
            "AND object_version_id !~ '[[:cntrl:]]')",
            name="ck_avatar_upload_object_version",
        ),
        Index("ix_avatar_upload_cleanup", "state", "last_attempted_at", "token"),
    )

    token: Mapped[str] = mapped_column(String(32), primary_key=True)
    # No user FK: an account purge must not discard physical cleanup evidence.
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    storage_ref: Mapped[dict] = mapped_column(JSONB(none_as_null=True), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    object_version_id: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    state: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
