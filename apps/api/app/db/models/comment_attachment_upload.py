"""Independent ownership survives comment, annotation and account deletion."""

from datetime import datetime
import uuid

from sqlalchemy import CheckConstraint, DateTime, Index, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

COMMENT_ATTACHMENT_STORAGE_REF_CHECK = """
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
    AND storage_ref->>'object_key' = 'comment-attachments/' || annotation_id::text || '/' || id::text
    AND jsonb_typeof(storage_ref->'storage_identity') = 'string'
    AND storage_ref->>'storage_identity' ~ '^[0-9a-f]{64}$'
), false)
"""
COMMENT_ATTACHMENT_FILE_NAME_CHECK = """
length(file_name) BETWEEN 1 AND 255
AND file_name NOT IN ('.','..')
AND position('/' in file_name) = 0
AND position(chr(92) in file_name) = 0
AND file_name !~ '[[:cntrl:]]'
AND file_name !~ ('[' || chr(127) || '-' || chr(159) || ']')
"""
COMMENT_ATTACHMENT_MIME_CHECK = """
length(mime_type) BETWEEN 3 AND 128
AND mime_type ~ '^[a-z0-9!#$%&''*+.^_`|~-]+/[a-z0-9!#$%&''*+.^_`|~-]+$'
"""


class CommentAttachmentUpload(Base):
    __tablename__ = "comment_attachment_uploads"
    __table_args__ = (
        CheckConstraint(
            "sha256 ~ '^[0-9a-f]{64}$'", name="ck_comment_attachment_sha256"
        ),
        CheckConstraint(
            "size_bytes BETWEEN 0 AND 20971520", name="ck_comment_attachment_size"
        ),
        CheckConstraint(
            COMMENT_ATTACHMENT_MIME_CHECK, name="ck_comment_attachment_mime_type"
        ),
        CheckConstraint(
            COMMENT_ATTACHMENT_FILE_NAME_CHECK, name="ck_comment_attachment_file_name"
        ),
        CheckConstraint(
            "state IN ('pending','ready','retired')", name="ck_comment_attachment_state"
        ),
        CheckConstraint(
            "expires_at = created_at + INTERVAL '2160 hours'",
            name="ck_comment_attachment_expiry",
        ),
        CheckConstraint(
            "(state='retired' AND retired_at IS NOT NULL AND retired_at >= created_at) OR (state<>'retired' AND retired_at IS NULL)",
            name="ck_comment_attachment_retirement",
        ),
        CheckConstraint(
            "state <> 'pending' OR bound_comment_id IS NULL",
            name="ck_comment_attachment_pending_unbound",
        ),
        CheckConstraint(
            COMMENT_ATTACHMENT_STORAGE_REF_CHECK,
            name="ck_comment_attachment_storage_ref",
        ),
        Index("ix_comment_attachment_cleanup", "state", "last_attempted_at", "id"),
        Index("ix_comment_attachment_bound_comment", "bound_comment_id"),
        Index("ix_comment_attachment_annotation", "annotation_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # Historical snapshots have no FK: business deletion cannot erase responsibility.
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    annotation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
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
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    bound_comment_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
