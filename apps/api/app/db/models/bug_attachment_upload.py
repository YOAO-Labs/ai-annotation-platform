"""Attachment ownership survives report deletion and account retention."""

from datetime import datetime
import uuid

from sqlalchemy import CheckConstraint, DateTime, Index, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

BUG_ATTACHMENT_STORAGE_REF_CHECK = """
COALESCE((
    jsonb_typeof(storage_ref) = 'object'
    AND storage_ref ?& ARRAY['schema','purpose','location_id','config_revision',
        'binding_generation','endpoint_url','bucket','object_key','storage_identity']
    AND storage_ref - ARRAY['schema','purpose','location_id','config_revision',
        'binding_generation','endpoint_url','bucket','object_key','storage_identity']::text[] = '{}'::jsonb
    AND storage_ref->'schema' = '1'::jsonb AND storage_ref->>'schema' = '1'
    AND storage_ref->>'purpose' = 'bug-reports'
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
    AND storage_ref->>'object_key' = 'bug-report-attachments/' || owner_id::text
        || '/' || id::text || CASE mime_type WHEN 'image/png' THEN '.png'
            WHEN 'image/jpeg' THEN '.jpg' WHEN 'image/webp' THEN '.webp' END
    AND jsonb_typeof(storage_ref->'storage_identity') = 'string'
    AND storage_ref->>'storage_identity' ~ '^[0-9a-f]{64}$'
), false)
"""

BUG_ATTACHMENT_FILE_NAME_CHECK = """
length(file_name) BETWEEN 1 AND 255
AND file_name NOT IN ('.','..')
AND position('/' in file_name) = 0
AND position(chr(92) in file_name) = 0
AND file_name !~ '[[:cntrl:]]'
AND file_name !~ ('[' || chr(127) || '-' || chr(159) || ']')
"""


class BugAttachmentUpload(Base):
    __tablename__ = "bug_attachment_uploads"
    __table_args__ = (
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_bug_attachment_sha256"),
        CheckConstraint(
            "size_bytes > 0 AND size_bytes <= 10485760", name="ck_bug_attachment_size"
        ),
        CheckConstraint(
            "mime_type IN ('image/png','image/jpeg','image/webp')",
            name="ck_bug_attachment_mime_type",
        ),
        CheckConstraint(
            BUG_ATTACHMENT_FILE_NAME_CHECK, name="ck_bug_attachment_file_name"
        ),
        CheckConstraint(
            "state IN ('pending','ready','retired')", name="ck_bug_attachment_state"
        ),
        CheckConstraint(
            "expires_at > created_at AND expires_at <= created_at + INTERVAL '4320 hours'",
            name="ck_bug_attachment_expiry",
        ),
        CheckConstraint(
            "(state='retired' AND retired_at IS NOT NULL) OR "
            "(state<>'retired' AND retired_at IS NULL)",
            name="ck_bug_attachment_retirement",
        ),
        CheckConstraint(
            "state <> 'pending' OR bound_report_id IS NULL",
            name="ck_bug_attachment_pending_unbound",
        ),
        CheckConstraint(
            BUG_ATTACHMENT_STORAGE_REF_CHECK, name="ck_bug_attachment_storage_ref"
        ),
        Index("ix_bug_attachment_cleanup", "state", "last_attempted_at", "id"),
        Index("ix_bug_attachment_bound_report", "bound_report_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # Snapshots have no FK: account/report deletion must retain physical facts.
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    storage_ref: Mapped[dict] = mapped_column(JSONB(none_as_null=True), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    mime_type: Mapped[str] = mapped_column(String(10), nullable=False)
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
    bound_report_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
