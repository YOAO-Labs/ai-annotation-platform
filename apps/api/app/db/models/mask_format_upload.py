"""Durable ownership of one Mask format upload, independent of its batch."""

from datetime import datetime
import uuid

from sqlalchemy import CheckConstraint, DateTime, Index, String, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def mask_staging_ref_check(column, logical_prefix, relative):
    """Build the same closed reference constraint for uploads and saved batches."""
    value = f"({column})"
    prefix = f"({logical_prefix})"
    root = (
        f"left({value}->>'key_prefix', length({value}->>'key_prefix')-length({prefix}))"
    )
    legacy = "4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7"
    return f"""
COALESCE((jsonb_typeof({value})='object'
    AND {value} ?& ARRAY['schema','purpose','location_id','config_revision','binding_generation','endpoint_url','bucket','key_prefix','storage_identity']
    AND {value} - ARRAY['schema','purpose','location_id','config_revision','binding_generation','endpoint_url','bucket','key_prefix','storage_identity']::text[]='{{}}'::jsonb
    AND {value}->>'purpose'='import'
    AND jsonb_typeof({value}->'endpoint_url')='string'
    AND {value}->>'endpoint_url' ~ '^https?://[^[:space:]/?#@]+$'
    AND position(chr(92) in ({value}->>'endpoint_url'))=0
    AND jsonb_typeof({value}->'bucket')='string'
    AND length({value}->>'bucket') BETWEEN 1 AND 255
    AND {value}->>'bucket' !~ '[[:space:]/?#@%:]'
    AND position(chr(92) in ({value}->>'bucket'))=0
    AND jsonb_typeof({value}->'key_prefix')='string'
    AND octet_length(({value}->>'key_prefix') || ({relative})) <= 1024
    AND jsonb_typeof({value}->'storage_identity')='string'
    AND {value}->>'storage_identity' ~ '^[0-9a-f]{{64}}$'
    AND (
      ({value}->'schema'='1'::jsonb AND {value}->>'schema'='1'
       AND {value}->>'location_id'='{legacy}'
       AND {value}->'config_revision'='1'::jsonb AND {value}->>'config_revision'='1'
       AND {value}->'binding_generation'='1'::jsonb AND {value}->>'binding_generation'='1'
       AND {value}->>'key_prefix'={prefix})
      OR ({value}->'schema'='2'::jsonb AND {value}->>'schema'='2'
       AND jsonb_typeof({value}->'location_id')='string'
       AND {value}->>'location_id' ~ '^[0-9a-f]{{8}}(-[0-9a-f]{{4}}){{3}}-[0-9a-f]{{12}}$'
       AND jsonb_typeof({value}->'config_revision')='number'
       AND {value}->>'config_revision' ~ '^[1-9][0-9]*$'
       AND jsonb_typeof({value}->'binding_generation')='number'
       AND {value}->>'binding_generation' ~ '^[1-9][0-9]*$'
       AND ({value}->>'binding_generation')::numeric >= 2
       AND (({value}->>'location_id'='{legacy}'
             AND {value}->>'config_revision'='1'
             AND {value}->>'key_prefix'={prefix})
         OR ({value}->>'location_id'<>'{legacy}'
             AND right({value}->>'key_prefix', length({prefix}))={prefix}
             AND {root} ~ '^([^/]+/)*_aap_managed/import/$'
             AND octet_length({root}) <= 645
             AND {value}->>'key_prefix' !~ '[[:cntrl:]]'
             AND position(chr(92) in ({value}->>'key_prefix'))=0
             AND {value}->>'key_prefix' !~ '(^|/)[.][.]?(/|$)|//')))
    )),false)
"""


UPLOAD_LOGICAL_PREFIX = "'mask-formats/' || project_id::text || '/' || uploader_id::text || '/' || id::text || '/'"
UPLOAD_RELATIVE = (
    f"substring(logical_object_key from length({UPLOAD_LOGICAL_PREFIX})+1)"
)
MASK_UPLOAD_REF_CHECK = mask_staging_ref_check(
    "storage_ref", UPLOAD_LOGICAL_PREFIX, UPLOAD_RELATIVE
)
BATCH_LOGICAL_PREFIX = "regexp_replace(staged_object_key, '[^/]+$', '')"
BATCH_RELATIVE = f"substring(staged_object_key from length({BATCH_LOGICAL_PREFIX})+1)"
MASK_BATCH_REF_CHECK = mask_staging_ref_check(
    "staging_ref", BATCH_LOGICAL_PREFIX, BATCH_RELATIVE
)


class MaskFormatUpload(Base):
    __tablename__ = "mask_format_uploads"
    __table_args__ = (
        CheckConstraint(
            f"left(logical_object_key, length({UPLOAD_LOGICAL_PREFIX}))={UPLOAD_LOGICAL_PREFIX} "
            f"AND {UPLOAD_RELATIVE} ~ '^[A-Za-z0-9._-]{{1,255}}$' "
            f"AND {UPLOAD_RELATIVE} NOT IN ('.','..')",
            name="ck_mask_format_upload_key",
        ),
        CheckConstraint(
            "upload_expires_at=created_at+interval '900 seconds'",
            name="ck_mask_format_upload_expiry",
        ),
        CheckConstraint(MASK_UPLOAD_REF_CHECK, name="ck_mask_format_upload_ref"),
        Index("ix_mask_format_upload_location", text("(storage_ref->>'location_id')")),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    # Account/project deletion must not erase a previously issued upload target.
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    uploader_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    logical_object_key: Mapped[str] = mapped_column(
        String(500), nullable=False, unique=True
    )
    content_type: Mapped[str] = mapped_column(String(255), nullable=False)
    storage_ref: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    upload_expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
