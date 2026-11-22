import uuid
from datetime import datetime
from sqlalchemy import (
    String,
    Integer,
    Float,
    Text,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base


INTERACTIVE_ACCEPT_PREDICTION_SOURCE = "interactive_accept"


class Prediction(Base):
    __tablename__ = "predictions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id"), index=True
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), index=True
    )
    ml_backend_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ml_backend_registry.id", ondelete="SET NULL")
    )
    ml_backend_pool_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ml_backend_service_pools.id", ondelete="SET NULL"),
    )
    model_version: Mapped[str | None] = mapped_column(String(100))
    score: Mapped[float | None] = mapped_column(Float)
    tool_unit_id: Mapped[str] = mapped_column(
        String(30), nullable=False, server_default="bbox", default="bbox"
    )
    result: Mapped[dict] = mapped_column(JSONB, nullable=False)
    cluster: Mapped[int | None] = mapped_column(Integer)
    mislabeling: Mapped[float | None] = mapped_column(Float)
    source: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        server_default="ml_backend",
        default="ml_backend",
        index=True,
    )
    rejected_shape_indexes: Mapped[list[int]] = mapped_column(
        JSONB,
        nullable=False,
        server_default="[]",
        default=list,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True, server_default=func.now()
    )


class PredictionMeta(Base):
    __tablename__ = "prediction_metas"
    __table_args__ = (
        ForeignKeyConstraint(
            ["prediction_id", "prediction_created_at"],
            ["predictions.id", "predictions.created_at"],
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    prediction_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), unique=True
    )
    prediction_created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    failed_prediction_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("failed_predictions.id")
    )
    inference_time_ms: Mapped[int | None] = mapped_column(Integer)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    total_tokens: Mapped[int | None] = mapped_column(Integer)
    prompt_cost: Mapped[float | None] = mapped_column(Float)
    completion_cost: Mapped[float | None] = mapped_column(Float)
    total_cost: Mapped[float | None] = mapped_column(Float)
    extra: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class FailedPrediction(Base):
    __tablename__ = "failed_predictions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id")
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), index=True
    )
    ml_backend_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ml_backend_registry.id", ondelete="SET NULL")
    )
    ml_backend_pool_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ml_backend_service_pools.id", ondelete="SET NULL"),
    )
    model_version: Mapped[str | None] = mapped_column(String(100))
    error_type: Mapped[str] = mapped_column(String(100), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    retry_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0", default=0
    )
    last_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dismissed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    extra: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default="{}", default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    execution_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("prediction_execution_snapshots.id", ondelete="RESTRICT"),
        index=True,
    )
