"""Explicit, directed grants to another accepted copy of the exact content."""

from datetime import datetime
import uuid

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class DatasetReferenceReplica(Base):
    __tablename__ = "dataset_reference_replicas"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    primary_asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("dataset_assets.id", ondelete="CASCADE")
    )
    replica_asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("dataset_assets.id", ondelete="CASCADE")
    )
    priority: Mapped[int] = mapped_column(default=100, server_default="100")
    created_by: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    revoked_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_reference_replica_primary", "primary_asset_id"),
        Index("ix_reference_replica_target", "replica_asset_id"),
        CheckConstraint(
            "primary_asset_id <> replica_asset_id AND priority BETWEEN 0 AND 1000",
            name="ck_reference_replica_target",
        ),
        CheckConstraint(
            "(revoked_at IS NULL) = (revoked_by IS NULL)",
            name="ck_reference_replica_revocation",
        ),
        Index(
            "uq_reference_replica_active",
            "primary_asset_id",
            "replica_asset_id",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )
