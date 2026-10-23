"""Storage tombstones survive deletion of the training records they belong to."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Index, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models.training_dataset import TRAINING_STORAGE_REF_CHECK


class TrainingStorageDeletion(Base):
    __tablename__ = "training_storage_deletions"
    __table_args__ = (
        UniqueConstraint(
            "bucket", "prefix", name="uq_training_storage_deletion_prefix"
        ),
        Index("ix_training_storage_deletion_attempt", "last_attempted_at", "id"),
        CheckConstraint(
            TRAINING_STORAGE_REF_CHECK, name="ck_training_deletion_storage_ref"
        ),
        CheckConstraint(
            "storage_ref IS NULL OR (storage_ref->>'bucket' = bucket "
            "AND storage_ref->>'prefix' = prefix)",
            name="ck_training_deletion_storage_locator",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    storage_ref: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    bucket: Mapped[str] = mapped_column(String(255), nullable=False)
    prefix: Mapped[str] = mapped_column(String(500), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
