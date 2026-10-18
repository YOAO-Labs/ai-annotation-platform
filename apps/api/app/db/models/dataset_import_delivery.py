from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class DatasetImportDelivery(Base):
    """Durable at-least-once handoff for connector-import media and notices."""

    __tablename__ = "dataset_import_deliveries"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('thumbnail', 'video_metadata', 'notification')",
            name="ck_dataset_import_deliveries_kind",
        ),
        CheckConstraint("attempts >= 0", name="ck_dataset_import_deliveries_attempts"),
        CheckConstraint(
            "(lease_token IS NULL) = (lease_expires_at IS NULL)",
            name="ck_dataset_import_deliveries_lease_pair",
        ),
        CheckConstraint(
            "delivered_at IS NULL OR lease_token IS NULL",
            name="ck_dataset_import_deliveries_delivered_lease",
        ),
        UniqueConstraint(
            "kind", "target_id", name="uq_dataset_import_deliveries_kind_target"
        ),
        Index(
            "ix_dataset_import_deliveries_pending_created",
            "created_at",
            "id",
            postgresql_where=text("delivered_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("pg_catalog.gen_random_uuid()"),
    )
    job_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    target_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    lease_token: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    delivered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
