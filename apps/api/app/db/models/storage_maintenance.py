"""Durable admission generations and unresolved physical storage operations."""

from datetime import datetime
import uuid

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class StorageMaintenanceGate(Base):
    __tablename__ = "storage_maintenance_gates"

    # Intentionally no business-location FK: independent admission must not wait
    # for a caller's uncommitted location or lose unresolved access responsibility.
    location_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    state: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="active"
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    changed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    __table_args__ = (
        CheckConstraint(
            "state IN ('active', 'draining', 'stopped')",
            name="ck_storage_maintenance_state",
        ),
        CheckConstraint("revision >= 1", name="ck_storage_maintenance_revision"),
    )


class StorageOperation(Base):
    __tablename__ = "storage_operations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    location_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("storage_maintenance_gates.location_id", ondelete="RESTRICT"),
        nullable=False,
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    purpose: Mapped[str] = mapped_column(String(64), nullable=False)
    node_id: Mapped[str] = mapped_column(String(128), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("revision >= 1", name="ck_storage_operation_revision"),
        Index(
            "ix_storage_operations_location_started", "location_id", "started_at", "id"
        ),
    )
