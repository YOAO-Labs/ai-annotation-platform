"""Immutable Assets catalog write receipts, retained past dataset deletion."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class DatasetCatalogOperation(Base):
    __tablename__ = "dataset_catalog_operations"
    __table_args__ = (
        CheckConstraint(
            "operation_kind IN ('create', 'update')",
            name="ck_dataset_catalog_operations_kind",
        ),
    )

    operation_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    actor_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    operation_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    dataset_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    request: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    result: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
