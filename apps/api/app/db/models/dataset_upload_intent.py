from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class DatasetUploadIntent(Base):
    """Assets-owned upload reservation and frozen completion/discard receipt."""

    __tablename__ = "dataset_upload_intents"

    item_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    dataset_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("datasets.id", ondelete="CASCADE"),
        nullable=False,
    )
    actor_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    file_name: Mapped[str] = mapped_column(String(500), nullable=False)
    file_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    file_type: Mapped[str] = mapped_column(String(20), nullable=False)
    content_type: Mapped[str] = mapped_column(String(255), nullable=False)
    staging_key: Mapped[str] = mapped_column(String(128), nullable=False)
    publication_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    state: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="pending"
    )
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "state IN ('pending', 'completed', 'discarded')",
            name="ck_dataset_upload_intents_state",
        ),
        CheckConstraint(
            "(publication_id IS NULL) = (file_path IS NULL)",
            name="ck_dataset_upload_intents_publication_pair",
        ),
        CheckConstraint(
            "file_path IS NULL OR char_length(file_path) <= 1000",
            name="ck_dataset_upload_intents_file_path_length",
        ),
        Index(
            "ix_dataset_upload_intents_dataset_state",
            "dataset_id",
            "state",
            "item_id",
        ),
    )
