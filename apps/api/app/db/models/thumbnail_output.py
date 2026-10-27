"""Independent saved thumbnail responsibility survives business deletion."""

from datetime import datetime
import uuid

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def media_cache_ref_check(logical_sql, column="storage_ref"):
    """Closed schema-two reference; logical_sql is a trusted schema expression."""
    c = column
    fields = "ARRAY['schema','purpose','location_id','config_revision','binding_generation','endpoint_url','bucket','prefix','storage_identity','key_prefix']"
    return f"""COALESCE((
        jsonb_typeof({c})='object' AND {c} ?& {fields}
        AND {c} - {fields}::text[]='{{}}'::jsonb
        AND {c}->'schema'='2'::jsonb AND {c}->>'schema'='2'
        AND {c}->>'purpose'='media-cache'
        AND jsonb_typeof({c}->'location_id')='string'
        AND {c}->>'location_id' ~ '^[0-9a-f]{{8}}(-[0-9a-f]{{4}}){{3}}-[0-9a-f]{{12}}$'
        AND jsonb_typeof({c}->'config_revision')='number'
        AND {c}->>'config_revision' ~ '^[1-9][0-9]*$'
        AND jsonb_typeof({c}->'binding_generation')='number'
        AND {c}->>'binding_generation' ~ '^[1-9][0-9]*$'
        AND jsonb_typeof({c}->'endpoint_url')='string'
        AND {c}->>'endpoint_url' ~ '^https?://[^[:space:]/?#@]+$'
        AND position(chr(92) in ({c}->>'endpoint_url'))=0
        AND jsonb_typeof({c}->'bucket')='string'
        AND length({c}->>'bucket') BETWEEN 1 AND 255
        AND {c}->>'bucket' !~ '[[:space:][:cntrl:]/?#@%:]'
        AND position(chr(92) in ({c}->>'bucket'))=0
        AND jsonb_typeof({c}->'prefix')='string'
        AND length({c}->>'prefix') BETWEEN 1 AND 1000
        AND octet_length({c}->>'prefix')<=1024
        AND jsonb_typeof({c}->'key_prefix')='string'
        AND octet_length({c}->>'key_prefix')<=436
        AND {c}->>'prefix'=({c}->>'key_prefix') || ({logical_sql})
        AND jsonb_typeof({c}->'storage_identity')='string'
        AND {c}->>'storage_identity' ~ '^[0-9a-f]{{64}}$'
        AND (({c}->>'location_id'='4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7'
              AND {c}->>'config_revision'='1' AND {c}->>'key_prefix'='')
          OR ({c}->>'location_id'<>'4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7'
              AND {c}->>'key_prefix' ~ '^([^/]+/)*_aap_managed/media-cache/$'
              AND {c}->>'key_prefix' !~ '[[:cntrl:]]|(^|/)[.][.]?(/|$)|//'
              AND position(chr(92) in ({c}->>'key_prefix'))=0))
    ),false)"""


class ThumbnailOutput(Base):
    __tablename__ = "thumbnail_outputs"
    __mapper_args__ = {"eager_defaults": True}
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    owner_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    owner_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    path_digest: Mapped[str | None] = mapped_column(String(64))
    input_ref: Mapped[dict] = mapped_column(JSONB(none_as_null=True), nullable=False)
    storage_ref: Mapped[dict] = mapped_column(JSONB(none_as_null=True), nullable=False)
    state: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="building"
    )
    sha256: Mapped[str | None] = mapped_column(String(64))
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    etag: Mapped[str | None] = mapped_column(String(1024))
    version_id: Mapped[str | None] = mapped_column(String(1024))
    writer_until: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_check_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    uncertain_write: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
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
            "owner_kind IN ('directory_preview','dataset_thumbnail','task_thumbnail')",
            name="ck_thumbnail_output_kind",
        ),
        CheckConstraint(
            "COALESCE((owner_kind='directory_preview' AND path_digest ~ '^[0-9a-f]{64}$') OR (owner_kind<>'directory_preview' AND path_digest IS NULL),false)",
            name="ck_thumbnail_output_path",
        ),
        CheckConstraint(
            "state IN ('building','ready','failed','retiring','deleted')",
            name="ck_thumbnail_output_state",
        ),
        CheckConstraint(
            "jsonb_typeof(input_ref)='object'", name="ck_thumbnail_output_input"
        ),
        CheckConstraint(
            media_cache_ref_check("'thumbnails/objects/' || id::text || '/'"),
            name="ck_thumbnail_output_storage_ref",
        ),
        CheckConstraint(
            "(sha256 IS NULL OR sha256 ~ '^[0-9a-f]{64}$') AND (size_bytes IS NULL OR size_bytes>=0) AND (state<>'ready' OR (sha256 IS NOT NULL AND size_bytes IS NOT NULL AND etag IS NOT NULL AND ready_at IS NOT NULL))",
            name="ck_thumbnail_output_receipt",
        ),
        Index("ix_thumbnail_outputs_owner", "owner_kind", "owner_id", "path_digest"),
        Index("ix_thumbnail_outputs_cleanup", "state", "next_check_at", "id"),
        Index("ix_thumbnail_outputs_location", storage_ref["location_id"].astext),
    )
