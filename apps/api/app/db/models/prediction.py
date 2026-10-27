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
    # v0.23.3 ADR-0050 · requested pool (单阶段 = 请求的池; 多阶段聚合 = root stage pool)。
    # ml_backend_id 永远表示实际执行的 selected registry instance; 多阶段聚合时每
    # stage / invocation 的双 ID lineage 存 PredictionMeta.extra.pipeline.selections[]。
    ml_backend_pool_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ml_backend_service_pools.id", ondelete="SET NULL"),
    )
    model_version: Mapped[str | None] = mapped_column(String(100))
    score: Mapped[float | None] = mapped_column(Float)
    # v0.10.17 · 与 Annotation.tool_unit_id 对齐; to_internal_shape 按 result.type 推断.
    tool_unit_id: Mapped[str] = mapped_column(
        String(30), nullable=False, server_default="bbox", default="bbox"
    )
    result: Mapped[dict] = mapped_column(JSONB, nullable=False)
    cluster: Mapped[int | None] = mapped_column(Integer)
    mislabeling: Mapped[float | None] = mapped_column(Float)
    # 区分普通待审预测、外部导入和交互式候选采纳后的溯源快照。
    # interactive_accept 只用于审计/模型溯源，不是待审候选。
    source: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        server_default="ml_backend",
        default="ml_backend",
        index=True,
    )
    # B-37 · 已驳回的 shape 下标列表（一个 Prediction.result 内含多个 shape，可逐个驳回）.
    # 持久化驳回状态，避免刷新后 AI 待审框重新出现.
    rejected_shape_indexes: Mapped[list[int]] = mapped_column(
        JSONB,
        nullable=False,
        server_default="[]",
        default=list,
    )
    # v0.10.25 · ADR-0006 Stage 2：predictions 按月 RANGE 分区，分区键 created_at 必须进 PK。
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True, server_default=func.now()
    )


class PredictionMeta(Base):
    __tablename__ = "prediction_metas"
    # v0.10.25 · 复合 FK 指向分区表 predictions(id, created_at)；prediction_created_at
    # 为冗余分区键列，写入路径在 prediction flush 后回填。
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
    # v0.23.3 ADR-0050 · 失败所在的 requested pool; 选择前失败 instance id 可为 null。
    ml_backend_pool_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ml_backend_service_pools.id", ondelete="SET NULL"),
    )
    model_version: Mapped[str | None] = mapped_column(String(100))
    error_type: Mapped[str] = mapped_column(String(100), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    # v0.8.6 F6 · 重试机制
    retry_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0", default=0
    )
    last_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # v0.8.8 · admin "永久放弃"软上限超过 max=3 的死项；soft-delete 不物理删
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
