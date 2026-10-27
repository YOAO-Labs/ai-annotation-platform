"""Retained whole-purpose cutovers and rollback native-fence responsibility."""

from datetime import datetime
import uuid

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class StorageMigrationCutover(Base):
    __tablename__ = "storage_migration_cutovers"
    __table_args__ = (
        CheckConstraint(
            "state IN ('active','rollback_draining','rollback_bound',"
            "'rollback_restoring','rolled_back')",
            name="ck_migration_cutover_state",
        ),
        CheckConstraint(
            "revision >= 1 AND target_gate_revision >= 1",
            name="ck_migration_cutover_revision",
        ),
        CheckConstraint(
            "review_sha256 ~ '^[a-f0-9]{64}$' AND octet_length(review::text) <= 131072 "
            "AND octet_length(before_bindings::text) <= 16384 "
            "AND octet_length(after_bindings::text) <= 16384 "
            "AND (rollback_claim IS NULL OR octet_length(rollback_claim::text) <= 4096) "
            "AND (rollback_native IS NULL OR octet_length(rollback_native::text) <= 65536)",
            name="ck_migration_cutover_budget",
        ),
        CheckConstraint(
            "(rollback_claim IS NULL) = (rollback_native IS NULL) AND "
            "(state IN ('rollback_bound','rollback_restoring','rolled_back')) = "
            "(rollback_claim IS NOT NULL AND rollback_native IS NOT NULL) "
            "AND (state IN ('rollback_restoring','rolled_back')) = "
            "(source_release_operation_id IS NOT NULL) "
            "AND (state = 'rolled_back') = (rolled_back_at IS NOT NULL)",
            name="ck_migration_cutover_phase_facts",
        ),
        Index("ix_migration_cutover_target", "target_location_id", "state"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("storage_migration_read_publications.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    window_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("storage_migration_windows.id", ondelete="RESTRICT"), unique=True
    )
    source_location_id: Mapped[uuid.UUID]
    target_location_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("storage_locations.id", ondelete="RESTRICT")
    )
    state: Mapped[str] = mapped_column(String(24))
    revision: Mapped[int]
    review: Mapped[dict] = mapped_column(JSONB)
    review_sha256: Mapped[str] = mapped_column(String(64))
    before_bindings: Mapped[dict] = mapped_column(JSONB)
    after_bindings: Mapped[dict] = mapped_column(JSONB)
    target_gate_revision: Mapped[int]
    rollback_claim: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    rollback_native: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    source_release_operation_id: Mapped[uuid.UUID | None]
    created_by: Mapped[uuid.UUID]
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    rolled_back_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
