"""Immutable native receipts for Assets-owned storage connection operations."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class StorageConnectionOperation(Base):
    __tablename__ = "storage_connection_operations"

    operation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True
    )
    actor_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    action: Mapped[str] = mapped_column(String(20), nullable=False)
    connection_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    intent: Mapped[dict] = mapped_column(JSONB, nullable=False)
    connection_scope: Mapped[str] = mapped_column(String(20), nullable=False)
    connection_created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    result: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "action IN ('create', 'update', 'delete', 'test')",
            name="ck_storage_connection_operations_action",
        ),
        CheckConstraint(
            "connection_scope IN ('global', 'owner')",
            name="ck_storage_connection_operations_scope",
        ),
        CheckConstraint(
            "jsonb_typeof(intent) = 'object'",
            name="ck_storage_connection_operations_intent",
        ),
        CheckConstraint(
            "jsonb_typeof(result) = 'object' "
            "AND result ->> 'operation_id' = operation_id::text "
            "AND result ->> 'actor_id' = actor_id::text "
            "AND result ->> 'action' = action "
            "AND result ->> 'connection_id' = connection_id::text",
            name="ck_storage_connection_operations_result",
        ),
    )
