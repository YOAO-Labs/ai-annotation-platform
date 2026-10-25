"""Durable immutable training input, independent of transient export jobs."""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    case,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    literal,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, column_property

from app.db.base import Base


# Both durable owners use the same non-secret locator envelope. Runtime
# resolution applies the stricter platform identity and canonical key checks.
TRAINING_STORAGE_REF_CHECK = """
storage_ref IS NULL OR COALESCE((
    jsonb_typeof(storage_ref) = 'object'
    AND storage_ref ?& ARRAY['schema','purpose','location_id','config_revision',
        'binding_generation','endpoint_url','bucket','prefix','storage_identity',
        'origin','evidence']
    AND storage_ref - ARRAY['schema','purpose','location_id','config_revision',
        'binding_generation','endpoint_url','bucket','prefix','storage_identity',
        'origin','evidence']::text[] = '{}'::jsonb
    AND storage_ref->'schema' = '1'::jsonb
    AND storage_ref->>'purpose' = 'training-dataset'
    AND jsonb_typeof(storage_ref->'location_id') = 'string'
    AND storage_ref->'config_revision' = '1'::jsonb
    AND storage_ref->'binding_generation' = '1'::jsonb
    AND jsonb_typeof(storage_ref->'endpoint_url') = 'string'
    AND storage_ref->>'endpoint_url' ~ '^https?://[^[:space:]/?#@]+$'
    AND jsonb_typeof(storage_ref->'bucket') = 'string'
    AND length(storage_ref->>'bucket') BETWEEN 1 AND 255
    AND storage_ref->>'bucket' !~ '[[:space:]/?#@]'
    AND position(chr(92) in (storage_ref->>'bucket')) = 0
    AND jsonb_typeof(storage_ref->'prefix') = 'string'
    AND storage_ref->>'prefix' ~ '^projects/[0-9a-f-]{36}/datasets/[0-9a-f-]{36}/$'
    AND jsonb_typeof(storage_ref->'storage_identity') = 'string'
    AND storage_ref->>'storage_identity' ~ '^[0-9a-f]{64}$'
    AND storage_ref->>'origin' IN ('new','verified-retained')
    AND (storage_ref->'evidence' = 'null'::jsonb
        OR jsonb_typeof(storage_ref->'evidence') = 'object')
    AND (storage_ref->>'origin' != 'new' OR storage_ref->'evidence' = 'null'::jsonb)
), false)
 OR COALESCE((
    jsonb_typeof(storage_ref)='object'
    AND storage_ref ?& ARRAY['schema','purpose','location_id','config_revision',
        'binding_generation','endpoint_url','bucket','prefix','storage_identity',
        'origin','evidence','key_prefix']
    AND storage_ref - ARRAY['schema','purpose','location_id','config_revision',
        'binding_generation','endpoint_url','bucket','prefix','storage_identity',
        'origin','evidence','key_prefix']::text[]='{}'::jsonb
    AND storage_ref->'schema'='2'::jsonb
    AND storage_ref->>'purpose'='training-dataset'
    AND jsonb_typeof(storage_ref->'location_id')='string'
    AND storage_ref->>'location_id' ~ '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$'
    AND jsonb_typeof(storage_ref->'config_revision')='number'
    AND storage_ref->>'config_revision' ~ '^[1-9][0-9]*$'
    AND jsonb_typeof(storage_ref->'binding_generation')='number'
    AND storage_ref->>'binding_generation' ~ '^[1-9][0-9]*$'
    AND jsonb_typeof(storage_ref->'endpoint_url')='string'
    AND storage_ref->>'endpoint_url' ~ '^https?://[^[:space:]/?#@]+$'
    AND position(chr(92) in (storage_ref->>'endpoint_url'))=0
    AND jsonb_typeof(storage_ref->'bucket')='string'
    AND length(storage_ref->>'bucket') BETWEEN 1 AND 255
    AND storage_ref->>'bucket' !~ '[[:space:][:cntrl:]/?#@%:]'
    AND position(chr(92) in (storage_ref->>'bucket'))=0
    AND jsonb_typeof(storage_ref->'key_prefix')='string'
    AND length(storage_ref->>'key_prefix')<=266
    AND octet_length(storage_ref->>'key_prefix')<=707
    AND jsonb_typeof(storage_ref->'prefix')='string'
    AND octet_length(storage_ref->>'prefix')<=1024
    AND left(storage_ref->>'prefix',length(storage_ref->>'key_prefix'))=storage_ref->>'key_prefix'
    AND substring(storage_ref->>'prefix' FROM length(storage_ref->>'key_prefix')+1)
        ~ '^projects/[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}/datasets/[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}/$'
    AND jsonb_typeof(storage_ref->'storage_identity')='string'
    AND storage_ref->>'storage_identity' ~ '^[0-9a-f]{64}$'
    AND storage_ref->>'origin'='new' AND storage_ref->'evidence'='null'::jsonb
    AND ((storage_ref->>'location_id'='4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7'
          AND storage_ref->'config_revision'='1'::jsonb AND storage_ref->>'key_prefix'='')
      OR (storage_ref->>'location_id'<>'4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7'
          AND (storage_ref->>'binding_generation')::numeric>=2
          AND storage_ref->>'key_prefix' ~ '^([^/]+/)*_aap_managed/training/$'
          AND storage_ref->>'key_prefix' !~ '[[:cntrl:]]|(^|/)[.][.]?(/|$)|//'
          AND position(chr(92) in (storage_ref->>'key_prefix'))=0))
),false)"""


class TrainingDatasetVersion(Base):
    __tablename__ = "training_dataset_versions"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "request_id", name="uq_training_dataset_request"
        ),
        CheckConstraint(
            "status IN ('pending','running','ready','failed','cancelled')",
            name="ck_training_dataset_status",
        ),
        Index("ix_training_dataset_project_created", "project_id", "created_at"),
        CheckConstraint(
            TRAINING_STORAGE_REF_CHECK, name="ck_training_dataset_storage_ref"
        ),
        CheckConstraint(
            "storage_ref IS NULL OR storage_ref->>'prefix' = COALESCE(storage_ref->>'key_prefix','') || "
            "'projects/' || project_id::text || '/datasets/' || id::text || '/'",
            name="ck_training_dataset_storage_prefix",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="RESTRICT"),
        nullable=False,
    )
    creator_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("training_dataset_versions.id", ondelete="RESTRICT"),
    )
    async_job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("async_jobs.id", ondelete="SET NULL")
    )
    request_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    attempt_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), default=uuid.uuid4, nullable=False
    )
    snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    task = column_property(
        case(
            (
                snapshot["task"]
                .as_string()
                .in_(["image-instance-segmentation", "segment"]),
                "segment",
            ),
            (snapshot["task"].as_string() == "image-detection", "detect"),
            else_=func.coalesce(snapshot["task"].as_string(), "detect"),
        )
    )
    media_type = column_property(
        func.coalesce(snapshot["media_type"].as_string(), "image")
    )
    format = column_property(
        func.coalesce(snapshot["format"].as_string(), "ultralytics-yolo")
    )
    source_media_type = column_property(
        func.coalesce(snapshot["source"]["media_type"].as_string(), "image")
    )
    snapshot_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    class_count = column_property(
        func.coalesce(func.jsonb_array_length(snapshot["classes"]), 0)
    )
    annotation_scope = column_property(
        func.coalesce(snapshot["annotation_scope"], literal({}, type_=JSONB))
    )
    preparation_report: Mapped[dict | None] = mapped_column(JSONB)
    summary = column_property(
        func.coalesce(
            manifest["summary"], preparation_report["summary"], literal({}, type_=JSONB)
        )
    )
    progress_pct: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(80))
    error_message: Mapped[str | None] = mapped_column(String(1000))
    storage_ref: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    package_key: Mapped[str | None] = mapped_column(String(500))
    package_sha256: Mapped[str | None] = mapped_column(String(64))
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
