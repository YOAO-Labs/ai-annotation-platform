"""Historical locator assertions survive business deletion; none permits cleanup."""

from datetime import datetime
import uuid

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    column,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ManagedOriginalRetirement(Base):
    __tablename__ = "managed_original_retirements"
    __table_args__ = (
        CheckConstraint("role = 'managed_original'", name="ck_managed_retirement_role"),
        CheckConstraint(
            "identity_state IN ('verified_binding', 'unknown')",
            name="ck_managed_retirement_identity_state",
        ),
        CheckConstraint(
            "(identity_state = 'verified_binding' AND storage_identity IS NOT NULL "
            "AND identity_error_code IS NULL) OR "
            "(identity_state = 'unknown' AND storage_identity IS NULL "
            "AND identity_error_code IS NOT NULL)",
            name="ck_managed_retirement_identity_evidence",
        ),
        CheckConstraint(
            "size_bytes IS NULL OR size_bytes >= 0", name="ck_managed_retirement_size"
        ),
        CheckConstraint(
            "sha256 IS NULL OR sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_managed_retirement_sha256",
        ),
        CheckConstraint(
            "policy_revision >= 1 AND dataset_lifecycle_revision >= 1",
            name="ck_managed_retirement_revisions",
        ),
        Index("ix_managed_retirement_location", "location_id", "retired_at", "id"),
        Index("ix_managed_retirement_asset", "asset_id"),
        Index(
            "ix_managed_retirement_locator_hash",
            func.md5(column("bucket")),
            func.md5(column("object_key")),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    # Immutable provenance of the original assertion. An audited offline restore
    # may remap physical fields without rewriting this digest or business IDs.
    assertion_digest: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True
    )
    # No business FKs: cascades must not erase retired locator responsibility.
    dataset_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    dataset_item_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    asset_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    creation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("managed_original_creations.id", ondelete="RESTRICT")
    )
    location_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    bucket: Mapped[str] = mapped_column(String(255), nullable=False)
    object_key: Mapped[str] = mapped_column(String(1000), nullable=False)
    role: Mapped[str] = mapped_column(String(30), nullable=False)
    # These are observations at retirement, not upload-time locator pins.
    location_config_revision: Mapped[int | None] = mapped_column()
    binding_generation: Mapped[int | None] = mapped_column()
    storage_identity: Mapped[str | None] = mapped_column(String(64))
    # verified_binding proves configuration agreement, never exclusive ownership
    # or current object existence/content. Unknown evidence does not block delete.
    identity_state: Mapped[str] = mapped_column(String(24), nullable=False)
    identity_error_code: Mapped[str | None] = mapped_column(String(64))
    asset_verification_state: Mapped[str] = mapped_column(String(20), nullable=False)
    sha256: Mapped[str | None] = mapped_column(String(64))
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    reference_state: Mapped[str] = mapped_column(String(20), nullable=False)
    etag: Mapped[str | None] = mapped_column(Text)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dataset_lifecycle_revision: Mapped[int] = mapped_column(nullable=False)
    retired_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    policy_revision: Mapped[int] = mapped_column(nullable=False, server_default="1")
    # No established retention policy means indefinite protection.
    retain_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
