"""Permanent archive ownership, independent of the job and source partition."""

from datetime import date, datetime
import uuid

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    Index,
    Integer,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

AUDIT_ARCHIVE_STORAGE_REF_CHECK = """
(COALESCE((
    jsonb_typeof(storage_ref) = 'object'
    AND storage_ref ?& ARRAY['schema','purpose','location_id','config_revision',
        'binding_generation','endpoint_url','bucket','object_key','storage_identity']
    AND storage_ref - ARRAY['schema','purpose','location_id','config_revision',
        'binding_generation','endpoint_url','bucket','object_key','storage_identity']::text[] = '{}'::jsonb
    AND storage_ref->'schema' = '1'::jsonb AND storage_ref->>'schema' = '1'
    AND storage_ref->>'purpose' = 'audit-archive'
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
    AND storage_ref->>'object_key' = year::text || '/' || lpad(month::text,2,'0') || '/' || id::text || '.jsonl.gz'
    AND jsonb_typeof(storage_ref->'storage_identity') = 'string'
    AND storage_ref->>'storage_identity' ~ '^[0-9a-f]{64}$'
), false)) OR (COALESCE((
    jsonb_typeof(storage_ref) = 'object'
    AND storage_ref ?& ARRAY['schema','purpose','location_id','config_revision',
        'binding_generation','endpoint_url','bucket','object_key','storage_identity']
    AND storage_ref - ARRAY['schema','purpose','location_id','config_revision',
        'binding_generation','endpoint_url','bucket','object_key','storage_identity']::text[] = '{}'::jsonb
    AND storage_ref->'schema' = '2'::jsonb AND storage_ref->>'schema' = '2'
    AND storage_ref->>'purpose' = 'audit-archive'
    AND jsonb_typeof(storage_ref->'location_id') = 'string' AND storage_ref->>'location_id' ~ '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$'
    AND jsonb_typeof(storage_ref->'config_revision') = 'number' AND storage_ref->>'config_revision' ~ '^[1-9][0-9]*$'
    AND jsonb_typeof(storage_ref->'binding_generation') = 'number' AND storage_ref->>'binding_generation' ~ '^[1-9][0-9]*$' AND (storage_ref->>'binding_generation')::numeric >= 2
    AND jsonb_typeof(storage_ref->'endpoint_url') = 'string'
    AND storage_ref->>'endpoint_url' ~ '^https?://[^[:space:]/?#@]+$'
    AND position(chr(92) in (storage_ref->>'endpoint_url')) = 0
    AND jsonb_typeof(storage_ref->'bucket') = 'string'
    AND length(storage_ref->>'bucket') BETWEEN 1 AND 255
    AND storage_ref->>'bucket' !~ '[[:space:]/?#@]'
    AND position(chr(92) in (storage_ref->>'bucket')) = 0
    AND jsonb_typeof(storage_ref->'object_key') = 'string'
    AND octet_length(storage_ref->>'object_key') <= 1024
    AND (
      (storage_ref->>'location_id' = '4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7'
       AND storage_ref->>'config_revision' = '1' AND storage_ref->>'object_key' = year::text || '/' || lpad(month::text,2,'0') || '/' || id::text || '.jsonl.gz')
      OR (storage_ref->>'location_id' <> '4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7'
       AND storage_ref->>'object_key' !~ '[[:cntrl:]]'
       AND position(chr(92) in (storage_ref->>'object_key')) = 0
       AND storage_ref->>'object_key' !~ '(^|/)[.][.]?(/|$)|//'
       AND storage_ref->>'object_key' ~ ('(^|/)_aap_managed/audit-archive/' || year::text || '/' || lpad(month::text,2,'0') || '/' || id::text || '[.]jsonl[.]gz$'))
    )
    AND jsonb_typeof(storage_ref->'storage_identity') = 'string'
    AND storage_ref->>'storage_identity' ~ '^[0-9a-f]{64}$'
), false))
"""

AUDIT_ARCHIVE_MONTH_CHECK = """
CASE WHEN year BETWEEN 2000 AND 9999 AND month BETWEEN 1 AND 12
    AND NOT (year=9999 AND month=12)
THEN source_schema='public'
    AND source_table='audit_logs_y' || year::text || 'm' || lpad(month::text,2,'0')
    AND month_start=make_date(year,month,1)
    AND month_end=(month_start + INTERVAL '1 month')::date
ELSE false END
"""


class AuditArchiveOutput(Base):
    __tablename__ = "audit_archive_outputs"
    __table_args__ = (
        CheckConstraint(AUDIT_ARCHIVE_MONTH_CHECK, name="ck_audit_archive_month"),
        CheckConstraint(
            "parent_oid BETWEEN 1 AND 4294967295 AND source_oid BETWEEN 1 AND 4294967295",
            name="ck_audit_archive_oids",
        ),
        CheckConstraint("format_revision=1", name="ck_audit_archive_format"),
        CheckConstraint(
            "row_count BETWEEN 0 AND 10000000", name="ck_audit_archive_rows"
        ),
        CheckConstraint(
            "source_size_bytes BETWEEN 0 AND 274877906944",
            name="ck_audit_archive_source_size",
        ),
        CheckConstraint(
            "size_bytes BETWEEN 1 AND 68719476736", name="ck_audit_archive_size"
        ),
        CheckConstraint(
            "source_sha256 ~ '^[0-9a-f]{64}$' AND sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_audit_archive_sha256",
        ),
        CheckConstraint("state IN ('pending','ready')", name="ck_audit_archive_state"),
        CheckConstraint(
            "(state='pending' AND published_at IS NULL) OR (state='ready' AND published_at IS NOT NULL AND published_at>=created_at)",
            name="ck_audit_archive_publication",
        ),
        CheckConstraint(
            AUDIT_ARCHIVE_STORAGE_REF_CHECK, name="ck_audit_archive_storage_ref"
        ),
        Index(
            "uq_audit_archive_ready_month",
            "year",
            "month",
            unique=True,
            postgresql_where=text("state='ready'"),
        ),
        Index("ix_audit_archive_job", "job_id"),
        Index("ix_audit_archive_location", text("(storage_ref->>'location_id')")),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # Historical snapshots must survive job deletion and database restoration.
    job_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    month: Mapped[int] = mapped_column(Integer, nullable=False)
    source_schema: Mapped[str] = mapped_column(
        String(63), nullable=False, default="public", server_default="public"
    )
    source_table: Mapped[str] = mapped_column(String(63), nullable=False)
    parent_oid: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_oid: Mapped[int] = mapped_column(BigInteger, nullable=False)
    month_start: Mapped[date] = mapped_column(Date, nullable=False)
    month_end: Mapped[date] = mapped_column(Date, nullable=False)
    format_revision: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    row_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    storage_ref: Mapped[dict] = mapped_column(JSONB(none_as_null=True), nullable=False)
    state: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
