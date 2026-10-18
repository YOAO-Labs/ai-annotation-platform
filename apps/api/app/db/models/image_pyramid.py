from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ImagePyramidAsset(Base):
    """Stable owner/profile identity for an immutable image-pyramid lineage."""

    __tablename__ = "image_pyramid_assets"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    dataset_item_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
    )
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
    )
    profile_version: Mapped[str] = mapped_column(String(64), nullable=False)
    active_generation: Mapped[int | None] = mapped_column(Integer, nullable=True)
    building_generation: Mapped[int | None] = mapped_column(Integer, nullable=True)
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
            "(dataset_item_id IS NOT NULL) <> (task_id IS NOT NULL)",
            name="ck_image_pyramid_assets_owner_xor",
        ),
        Index(
            "uq_image_pyramid_assets_dataset_profile",
            "dataset_item_id",
            "profile_version",
            unique=True,
            postgresql_where=text("dataset_item_id IS NOT NULL"),
        ),
        Index(
            "uq_image_pyramid_assets_task_profile",
            "task_id",
            "profile_version",
            unique=True,
            postgresql_where=text("task_id IS NOT NULL"),
        ),
    )


class ImagePyramidGeneration(Base):
    """One immutable attempt beneath an :class:`ImagePyramidAsset`."""

    __tablename__ = "image_pyramid_generations"

    asset_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("image_pyramid_assets.id", ondelete="CASCADE"),
        nullable=False,
    )
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    job_generation: Mapped[int | None] = mapped_column(BigInteger)
    source_generation: Mapped[int | None] = mapped_column(BigInteger)
    source_snapshot: Mapped[str | None] = mapped_column(String(64))
    output_prefix: Mapped[str | None] = mapped_column(String(1024))
    manifest: Mapped[dict | None] = mapped_column(JSONB)
    policy: Mapped[dict | None] = mapped_column(JSONB)
    source_identity: Mapped[str] = mapped_column(String(255), nullable=False)
    source_fingerprint: Mapped[str | None] = mapped_column(String(80), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    lease_token: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tile_size: Mapped[int] = mapped_column(
        Integer, nullable=False, default=512, server_default="512"
    )
    overlap: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    max_level: Mapped[int | None] = mapped_column(Integer, nullable=True)
    format: Mapped[str | None] = mapped_column(String(16), nullable=True)
    normalization_version: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    manifest_digest: Mapped[str | None] = mapped_column(String(80), nullable=True)
    overview_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    overview_width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    overview_height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    overview_digest: Mapped[str | None] = mapped_column(String(80), nullable=True)
    tile_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    retained_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    last_accessed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
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
        PrimaryKeyConstraint(
            "asset_id", "generation", name="pk_image_pyramid_generations"
        ),
        CheckConstraint("generation > 0", name="ck_image_pyramid_generation_positive"),
        CheckConstraint(
            "status IN ('pending', 'building', 'ready', 'failed')",
            name="ck_image_pyramid_generation_status",
        ),
        CheckConstraint("tile_size > 0", name="ck_image_pyramid_tile_size_positive"),
        CheckConstraint("overlap >= 0", name="ck_image_pyramid_overlap_nonnegative"),
        Index(
            "uq_image_pyramid_generation_inflight",
            "asset_id",
            unique=True,
            postgresql_where=text("status IN ('pending', 'building')"),
        ),
        Index(
            "ix_image_pyramid_generations_status_updated",
            "status",
            "updated_at",
        ),
    )
