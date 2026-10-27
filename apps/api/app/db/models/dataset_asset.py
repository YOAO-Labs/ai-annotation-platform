"""Content identities and pinned locations; credentials stay with their owners."""

from datetime import datetime
import uuid

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


DATASET_STORAGE_REF_CHECK = r"""
storage_ref IS NULL OR COALESCE((
    jsonb_typeof(storage_ref)='object'
    AND storage_ref ?& ARRAY['schema','purpose','location_id','config_revision',
        'binding_generation','endpoint_url','bucket','key_prefix','object_key','storage_identity']
    AND storage_ref - ARRAY['schema','purpose','location_id','config_revision',
        'binding_generation','endpoint_url','bucket','key_prefix','object_key','storage_identity']::text[]='{}'::jsonb
    AND storage_ref->'schema'='1'::jsonb AND storage_ref->>'schema'='1'
    AND storage_ref->>'purpose'='datasets'
    AND jsonb_typeof(storage_ref->'location_id')='string'
    AND storage_ref->>'location_id'=location_id::text
    AND jsonb_typeof(storage_ref->'config_revision')='number'
    AND storage_ref->>'config_revision' ~ '^[1-9][0-9]*$'
    AND jsonb_typeof(storage_ref->'binding_generation')='number'
    AND storage_ref->>'binding_generation' ~ '^[1-9][0-9]*$'
    AND jsonb_typeof(storage_ref->'endpoint_url')='string'
    AND storage_ref->>'endpoint_url' ~ '^https?://[^[:space:]/?#@]+$'
    AND position(chr(92) in (storage_ref->>'endpoint_url'))=0
    AND jsonb_typeof(storage_ref->'bucket')='string'
    AND storage_ref->>'bucket'=bucket
    AND length(bucket) BETWEEN 1 AND 255
    AND bucket !~ '[[:space:][:cntrl:]/?#@%:]' AND position(chr(92) in bucket)=0
    AND jsonb_typeof(storage_ref->'object_key')='string'
    AND storage_ref->>'object_key'=object_key
    AND length(object_key) BETWEEN 1 AND 1000 AND octet_length(object_key)<=1024
    AND object_key !~ '[[:cntrl:]]'
    AND jsonb_typeof(storage_ref->'key_prefix')='string'
    AND length(storage_ref->>'key_prefix')<=891
    AND octet_length(storage_ref->>'key_prefix')<=915
    AND jsonb_typeof(storage_ref->'storage_identity')='string'
    AND storage_ref->>'storage_identity' ~ '^[0-9a-f]{64}$'
    AND ((location_id='4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7'::uuid
          AND storage_ref->>'config_revision'='1' AND storage_ref->>'key_prefix'='')
      OR (location_id<>'4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7'::uuid
          AND storage_ref->>'key_prefix' ~ '^([^/]+/)*_aap_managed/datasets/$'
          AND storage_ref->>'key_prefix' !~ '[[:cntrl:]]|(^|/)[.][.]?(/|$)|//'
          AND position(chr(92) in (storage_ref->>'key_prefix'))=0
          AND left(object_key,length(storage_ref->>'key_prefix'))=storage_ref->>'key_prefix'
          AND length(object_key)>length(storage_ref->>'key_prefix')))
),false)
"""


class StorageLocation(Base):
    __tablename__ = "storage_locations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    config_revision: Mapped[int] = mapped_column(nullable=False, server_default="1")
    config: Mapped[dict] = mapped_column(JSONB, nullable=False)
    connection_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("storage_connections.id", ondelete="CASCADE"), unique=True
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true"
    )


class StorageLocationProbe(Base):
    __tablename__ = "storage_location_probes"

    location_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("storage_locations.id", ondelete="CASCADE"), primary_key=True
    )
    node_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    config_revision: Mapped[int] = mapped_column(nullable=False)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    result: Mapped[dict] = mapped_column(JSONB, nullable=False)


class StorageBinding(Base):
    __tablename__ = "storage_bindings"

    purpose: Mapped[str] = mapped_column(String(30), primary_key=True)
    location_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("storage_locations.id"), nullable=False
    )
    bucket: Mapped[str] = mapped_column(String(255), nullable=False)
    generation: Mapped[int] = mapped_column(nullable=False, server_default="1")


class DatasetAsset(Base):
    __tablename__ = "dataset_assets"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    dataset_item_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("dataset_items.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    media_type: Mapped[str] = mapped_column(String(20), nullable=False)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    checksum_algorithm: Mapped[str | None] = mapped_column(String(20))
    checksum_value: Mapped[str | None] = mapped_column(String(64), index=True)
    verification_state: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="unverified"
    )
    legacy_hash_hint: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "size_bytes IS NULL OR size_bytes >= 0", name="ck_dataset_assets_size"
        ),
        CheckConstraint(
            "(verification_state = 'unverified' AND checksum_algorithm IS NULL AND checksum_value IS NULL) OR (verification_state = 'verified' AND checksum_algorithm IS NOT NULL AND checksum_value IS NOT NULL AND checksum_algorithm = 'sha256' AND checksum_value ~ '^[0-9a-f]{64}$' AND size_bytes IS NOT NULL)",
            name="ck_dataset_assets_verification",
        ),
    )


class DatasetAssetLocation(Base):
    __tablename__ = "dataset_asset_locations"

    asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("dataset_assets.id", ondelete="CASCADE"), primary_key=True
    )
    location_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("storage_locations.id"), primary_key=True
    )
    bucket: Mapped[str] = mapped_column(String(255), primary_key=True)
    object_key: Mapped[str] = mapped_column(String(1000), nullable=False)
    role: Mapped[str] = mapped_column(
        String(30), nullable=False, server_default="managed_original"
    )
    reference_state: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="unverified"
    )
    creation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("managed_original_creations.id", ondelete="RESTRICT")
    )
    storage_ref: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    etag: Mapped[str | None] = mapped_column(Text)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            DATASET_STORAGE_REF_CHECK, name="ck_dataset_asset_location_storage_ref"
        ),
        Index(
            "ix_asset_location_usage_locator", func.md5(bucket), func.md5(object_key)
        ),
        UniqueConstraint(
            "asset_id",
            "location_id",
            "bucket",
            "object_key",
            name="uq_dataset_asset_location_locator",
        ),
        CheckConstraint(
            "reference_state IN ('unverified', 'present', 'missing', 'changed')",
            name="ck_dataset_asset_location_state",
        ),
    )
