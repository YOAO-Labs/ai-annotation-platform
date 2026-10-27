"""Migration read windows retain responsibility independently of job retention."""

from datetime import datetime
import uuid

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class StorageMigrationWindow(Base):
    __tablename__ = "storage_migration_windows"
    __table_args__ = (
        CheckConstraint(
            "state IN ('draining','stopped','closing','closed')",
            name="ck_migration_window_state",
        ),
        CheckConstraint(
            "revision >= 1 AND source_gate_revision >= 1 AND source_config_revision >= 1",
            name="ck_migration_window_revisions",
        ),
        Index(
            "uq_migration_window_active_source",
            "source_location_id",
            unique=True,
            postgresql_where=text("state <> 'closed'"),
        ),
        Index("ix_migration_window_history", "created_at", "id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    source_location_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("storage_maintenance_gates.location_id", ondelete="RESTRICT")
    )
    source: Mapped[dict] = mapped_column(JSONB)
    source_config_revision: Mapped[int]
    parent_copy_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("storage_migration_copies.id", ondelete="RESTRICT")
    )
    target_location_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("storage_locations.id", ondelete="RESTRICT")
    )
    target: Mapped[dict] = mapped_column(JSONB)
    state: Mapped[str] = mapped_column(String(20), server_default="draining")
    revision: Mapped[int] = mapped_column(server_default="1")
    source_gate_revision: Mapped[int]
    created_by: Mapped[uuid.UUID | None]
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
