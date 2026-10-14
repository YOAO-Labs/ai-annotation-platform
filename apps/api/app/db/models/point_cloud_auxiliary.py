"""Immutable auxiliary generations shared by tasks with identical inputs."""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class PointCloudAuxiliaryAsset(Base):
    __tablename__ = "point_cloud_auxiliary_assets"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    dataset_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dataset_items.id", ondelete="CASCADE"),
        nullable=False,
    )
    binding_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    profile: Mapped[str] = mapped_column(String(64), nullable=False)
    active_generation: Mapped[int | None] = mapped_column(Integer)
    building_generation: Mapped[int | None] = mapped_column(Integer)
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
        UniqueConstraint(
            "dataset_item_id",
            "binding_digest",
            "profile",
            name="uq_point_cloud_auxiliary_owner_binding_profile",
        ),
        CheckConstraint(
            "char_length(binding_digest) = 64",
            name="ck_point_cloud_auxiliary_binding_digest",
        ),
        CheckConstraint(
            "active_generation IS NULL OR active_generation > 0",
            name="ck_point_cloud_auxiliary_active_generation",
        ),
        CheckConstraint(
            "building_generation IS NULL OR building_generation > 0",
            name="ck_point_cloud_auxiliary_building_generation",
        ),
    )


class PointCloudAuxiliaryGeneration(Base):
    __tablename__ = "point_cloud_auxiliary_generations"

    asset_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("point_cloud_auxiliary_assets.id", ondelete="CASCADE"),
        nullable=False,
    )
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    input_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    source_manifest: Mapped[dict] = mapped_column(JSONB, nullable=False)
    provenance: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    cameras: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    lease_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_detail: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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
        PrimaryKeyConstraint("asset_id", "generation"),
        CheckConstraint("generation > 0", name="ck_point_cloud_auxiliary_generation"),
        CheckConstraint(
            "char_length(input_digest) = 64",
            name="ck_point_cloud_auxiliary_input_digest",
        ),
        CheckConstraint(
            "status IN ('building', 'ready', 'failed')",
            name="ck_point_cloud_auxiliary_status",
        ),
        Index(
            "uq_point_cloud_auxiliary_inflight",
            "asset_id",
            unique=True,
            postgresql_where=text("status = 'building'"),
        ),
        Index("ix_point_cloud_auxiliary_status_updated", "status", "updated_at"),
    )
