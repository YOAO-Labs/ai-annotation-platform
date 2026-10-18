"""Immutable Assets receipts for idempotent spatial-state mutations."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class DatasetSpatialOperation(Base):
    """Assets-owned operation receipts retained beyond resource deletion."""

    __tablename__ = "dataset_spatial_operations"
    __table_args__ = (
        CheckConstraint(
            "action IN ("
            "'scene.patch', 'layout.apply', 'pose.upsert', 'scene.create', "
            "'items.assign', 'scene.source.set', 'calibration.update', "
            "'pointcloud.prepare', 'calibration.provenance'"
            ")",
            name="ck_dataset_spatial_operations_action",
        ),
        CheckConstraint(
            "jsonb_typeof(scope) = 'object'",
            name="ck_dataset_spatial_operations_scope_object",
        ),
        CheckConstraint(
            "jsonb_typeof(request) = 'object'",
            name="ck_dataset_spatial_operations_request_object",
        ),
        CheckConstraint(
            "jsonb_typeof(result) = 'object'",
            name="ck_dataset_spatial_operations_result_object",
        ),
    )

    operation_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    actor_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    scope: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    request: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    result: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
