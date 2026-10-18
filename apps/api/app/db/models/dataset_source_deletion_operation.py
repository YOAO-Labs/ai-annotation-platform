"""Assets-owned deletion receipts survive removal of their source catalog rows."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import DateTime, func
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class DatasetSourceDeletionOperation(Base):
    __tablename__ = "dataset_source_deletion_operations"

    operation_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    actor_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    dataset_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    item_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    request: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    result: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
