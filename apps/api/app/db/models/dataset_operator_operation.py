from datetime import datetime
import uuid

from sqlalchemy import CheckConstraint, DateTime, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class DatasetOperatorOperation(Base):
    """Immutable trusted operator receipts retained beyond Dataset deletion."""

    __tablename__ = "dataset_operator_operations"

    operation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True
    )
    action: Mapped[str] = mapped_column(Text, nullable=False)
    intent: Mapped[dict] = mapped_column(JSONB, nullable=False)
    result: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "action IN ('create_image_dataset', 'ensure_nuscenes_dataset', "
            "'append_items', 'patch_items', 'recount_items')",
            name="ck_dataset_operator_operations_action",
        ),
        CheckConstraint(
            "jsonb_typeof(intent) = 'object'",
            name="ck_dataset_operator_operations_intent",
        ),
        CheckConstraint(
            "jsonb_typeof(result) = 'object' "
            "AND result ->> 'schema' = 'aap-assets-operator/v1' "
            "AND result -> 'ok' = 'true'::jsonb "
            "AND jsonb_typeof(result -> 'data') = 'object'",
            name="ck_dataset_operator_operations_result",
        ),
    )
