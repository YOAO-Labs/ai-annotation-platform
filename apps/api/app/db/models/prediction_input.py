"""Independent recovery records for one prediction attempt's temporary inputs."""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
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
from app.db.models.mask_format_upload import mask_staging_ref_check


_LEGACY_PREDICTION_INPUT_REF_CHECK = r"""
COALESCE((jsonb_typeof(storage_ref)='object'
    AND storage_ref ?& ARRAY['schema','purpose','location_id','config_revision','binding_generation','endpoint_url','bucket','key_prefix','storage_identity']
    AND storage_ref - ARRAY['schema','purpose','location_id','config_revision','binding_generation','endpoint_url','bucket','key_prefix','storage_identity']::text[]='{}'::jsonb
    AND storage_ref->'schema'='1'::jsonb AND storage_ref->>'schema'='1'
    AND storage_ref->>'purpose'='import'
    AND storage_ref->>'location_id'='4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7'
    AND storage_ref->'config_revision'='1'::jsonb AND storage_ref->>'config_revision'='1'
    AND storage_ref->'binding_generation'='1'::jsonb AND storage_ref->>'binding_generation'='1'
    AND jsonb_typeof(storage_ref->'endpoint_url')='string'
    AND storage_ref->>'endpoint_url' ~ '^https?://[^[:space:]/?#@]+$'
    AND position(chr(92) in (storage_ref->>'endpoint_url'))=0
    AND jsonb_typeof(storage_ref->'bucket')='string'
    AND length(storage_ref->>'bucket') BETWEEN 1 AND 255
    AND storage_ref->>'bucket' !~ '[[:space:]/?#@%:]'
    AND position(chr(92) in (storage_ref->>'bucket'))=0
    AND storage_ref->>'bucket'=bucket
    AND storage_ref->>'key_prefix'='prediction-inputs/' || id::text || '/'
    AND jsonb_typeof(storage_ref->'storage_identity')='string'
    AND storage_ref->>'storage_identity' ~ '^[0-9a-f]{64}$'
    AND storage_ref->>'storage_identity'=storage_identity),false)
"""


PREDICTION_INPUT_REF_CHECK = (
    "("
    + _LEGACY_PREDICTION_INPUT_REF_CHECK
    + ") OR (storage_ref->'schema'='2'::jsonb AND "
    + mask_staging_ref_check(
        "storage_ref", "'prediction-inputs/' || id::text || '/'", "''"
    )
    + " AND storage_ref->>'bucket'=bucket AND storage_ref->>'storage_identity'=storage_identity)"
)


class PredictionInputLease(Base):
    __tablename__ = "prediction_input_leases"
    __table_args__ = (
        CheckConstraint(
            "state IN ('active', 'cleanup_pending', 'deleted')",
            name="ck_prediction_input_lease_state",
        ),
        CheckConstraint(
            "contract_version IN (1,2,3)", name="ck_prediction_input_contract"
        ),
        CheckConstraint(
            "(contract_version IN (1,2) AND object_count BETWEEN 0 AND 10000 AND size_bytes BETWEEN 0 AND 4294967296) OR (contract_version=3 AND object_count BETWEEN 0 AND 1 AND size_bytes BETWEEN 0 AND 5368709120)",
            name="ck_prediction_input_budget",
        ),
        CheckConstraint(
            "contract_version=1 OR (expires_at=created_at+interval '2 hours' "
            "AND retain_until=expires_at+interval '7 days' "
            "AND ((state='deleted' AND deleted_at IS NOT NULL AND deleted_at>=created_at) "
            "OR (state<>'deleted' AND deleted_at IS NULL)))",
            name="ck_prediction_input_times",
        ),
        CheckConstraint(
            "(contract_version=1 AND owner_kind NOT IN ('video_batch_predict','video_prediction_retry','video_predict_test','video_interactive','video_tracker')) OR (contract_version=2 AND owner_kind IN ('batch_predict','prediction_retry','interactive_predict','predict_test','mask_repair','secondary_inference','frame_predict','frame_interactive')) OR (contract_version=3 AND owner_kind IN ('video_batch_predict','video_prediction_retry','video_predict_test','video_interactive','video_tracker'))",
            name="ck_prediction_input_owner",
        ),
        CheckConstraint(
            "(contract_version=1 AND storage_ref IS NULL AND maintenance_revision IS NULL AND inventory IS NULL AND source_ref IS NULL AND object_count=0 AND size_bytes=0 AND deleted_at IS NULL AND state<>'deleted') OR (contract_version IN (2,3) AND maintenance_revision>=1 AND maintenance_revision IS NOT NULL AND "
            + PREDICTION_INPUT_REF_CHECK
            + ")",
            name="ck_prediction_input_pin",
        ),
        CheckConstraint(
            "contract_version=1 OR COALESCE((jsonb_typeof(inventory)='object' AND inventory ?& ARRAY['schema','objects'] AND inventory-ARRAY['schema','objects']::text[]='{}'::jsonb AND inventory->'schema'='1'::jsonb AND inventory->>'schema'='1' AND jsonb_typeof(inventory->'objects')='object' AND octet_length(inventory::text)<=4194304 AND (source_ref IS NULL OR (jsonb_typeof(source_ref)='object' AND octet_length(source_ref::text)<=1048576))),false)",
            name="ck_prediction_input_documents",
        ),
        Index("ix_prediction_input_cleanup", "next_cleanup_at", "expires_at"),
        Index("ix_prediction_input_location", text("(storage_ref->>'location_id')")),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    # Deliberately no cascading FKs: deleting a business owner cannot erase cleanup.
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    owner_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    contract_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=2, server_default="2"
    )
    storage_ref: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    maintenance_revision: Mapped[int | None] = mapped_column(Integer)
    source_ref: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    inventory: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    object_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    size_bytes: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    bucket: Mapped[str] = mapped_column(String(255), nullable=False)
    storage_identity: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="active")
    uncertain_write: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    retain_until: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    next_cleanup_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_cleanup_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(64))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
