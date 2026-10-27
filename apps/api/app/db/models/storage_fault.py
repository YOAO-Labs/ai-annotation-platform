"""Sparse source-fault observations, independent of business-row lifetimes."""

from datetime import datetime
import uuid

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Index, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class StorageFaultState(Base):
    __tablename__ = "storage_fault_states"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    scope_key: Mapped[str] = mapped_column(String(64), unique=True)
    # No parent FK: the bounded observer must not wait on a caller's parent locks.
    location_id: Mapped[uuid.UUID] = mapped_column(index=True)
    source_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True, index=True)
    category: Mapped[str] = mapped_column(String(24))
    operation: Mapped[str] = mapped_column(String(24))
    node_id: Mapped[str] = mapped_column(String(255))
    state: Mapped[str] = mapped_column(String(12))
    first_failed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_failed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    recovered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_count: Mapped[int] = mapped_column(BigInteger)
    last_sequence: Mapped[int] = mapped_column(BigInteger)
    recovered_sequence: Mapped[int | None] = mapped_column(BigInteger)
    scope_facts: Mapped[dict] = mapped_column(JSONB)
    evidence: Mapped[dict] = mapped_column(JSONB)

    __table_args__ = (
        CheckConstraint(
            "category IN ('root_unavailable','access_denied','source_changed','preview_failed')",
            name="ck_storage_fault_category",
        ),
        CheckConstraint(
            "state IN ('active','recovered')", name="ck_storage_fault_state"
        ),
        CheckConstraint(
            "failure_count > 0 AND last_sequence > 0 AND "
            "(recovered_sequence IS NULL OR (recovered_sequence > 0 AND recovered_sequence <= last_sequence))",
            name="ck_storage_fault_watermark",
        ),
        CheckConstraint(
            "(recovered_at IS NULL) = (recovered_sequence IS NULL) AND "
            "(state <> 'recovered' OR recovered_at IS NOT NULL)",
            name="ck_storage_fault_recovery",
        ),
        Index(
            "ix_storage_fault_location_state", "location_id", "state", "last_failed_at"
        ),
    )
