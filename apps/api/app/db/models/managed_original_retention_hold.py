"""Operator-declared requirements; references never certify recovery readiness."""

from datetime import datetime
import uuid

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ManagedOriginalRetentionHold(Base):
    __tablename__ = "managed_original_retention_holds"
    __table_args__ = (
        CheckConstraint("kind IN ('backup', 'migration')", name="ck_managed_hold_kind"),
        CheckConstraint(
            "reference_code ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,99}$'",
            name="ck_managed_hold_reference",
        ),
        CheckConstraint("reason ~ '[^[:space:]]'", name="ck_managed_hold_reason"),
        CheckConstraint(
            "(revision = 1 AND released_at IS NULL AND released_by IS NULL AND release_reason IS NULL) "
            "OR (revision = 2 AND released_at IS NOT NULL AND released_by IS NOT NULL "
            "AND release_reason IS NOT NULL AND release_reason ~ '[^[:space:]]')",
            name="ck_managed_hold_release",
        ),
        Index("ix_managed_hold_anchor", "retirement_id", "created_at", "id"),
        Index(
            "uq_managed_hold_active_reference",
            "retirement_id",
            "kind",
            "reference_code",
            unique=True,
            postgresql_where=text("released_at IS NULL"),
        ),
    )

    # Client-generated and preview-bound; a lost response cannot create a second ID.
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    retirement_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("managed_original_retirements.id", ondelete="RESTRICT"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(String(12), nullable=False)
    reference_code: Mapped[str] = mapped_column(String(100), nullable=False)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    # Historical identities have no actor FK or cascade.
    created_by: Mapped[uuid.UUID] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    revision: Mapped[int] = mapped_column(nullable=False, server_default="1")
    release_reason: Mapped[str | None] = mapped_column(String(500))
    released_by: Mapped[uuid.UUID | None] = mapped_column()
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
