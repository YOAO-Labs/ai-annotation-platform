"""v0.10.16 · 统一异步任务表 async_jobs（ROADMAP §1.7）。

所有长任务（batch_predict / video_tracker / audit_archive / predictions_import /
export 等）在 enqueue / progress / finish 三时点写入此表。

v0.10.49 · batch_predict 已收敛为单一真值（prediction_jobs 专表删除），
domain 字段进 payload/result JSONB。video_tracker 保留 video_tracker_jobs 专表
（带活标注 FK + 运行时状态机，见 ROADMAP/2026-05-23-async-jobs-unification.md）。
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AsyncJobKind(str, enum.Enum):
    """v0.10.16 · 异步任务类型枚举。新加 kind 时同时更新前端 i18n 与铃铛文案。"""

    BATCH_PREDICT = "batch_predict"  # 项目 / 批次预标
    VIDEO_TRACKER = "video_tracker"  # 视频追踪
    VIDEO_CORRECTION = "video_correction"  # 视频 Mask 纠错传播
    AUDIT_ARCHIVE = "audit_archive"  # 审计日志月分区归档
    PREDICTIONS_IMPORT = "predictions_import"  # 外部 prediction 导入
    PREDICTION_RETRY = "prediction_retry"  # 失败预测重试
    EXPORT = "export"  # 标注导出（异步化 + 目录镜像）
    TRAINING_DATASET = "training_dataset"
    TRAINING_RUN = "training_run"
    TRAINING_PREVIEW = "training_preview"
    SAMPLE_EVALUATION = "sample_evaluation"
    ACTIVE_LEARNING_ROUND = "active_learning_round"
    STORAGE_USAGE = "storage_usage"
    VIDEO_ASSET_RETRY = "video_asset_retry"
    MANAGED_ORIGINAL_VERIFICATION = "managed_original_verification"
    DATASET_QUALITY = "dataset_quality"
    DATASET_QUALITY_REVIEW = "dataset_quality_review"
    DATASET_SCAN = "dataset_scan"
    DATASET_REGISTRATION = "dataset_registration"
    DATASET_IMPORT = "dataset_import"  # 外部连接器数据集导入
    CREATE_TASKS = "create_tasks"  # dataset link → 建 task
    MASK_QC = "mask_qc"  # Raster / Video Mask 异步质量扫描
    VIDEO_TRACK_QUALITY = "video_track_quality"  # 相邻视频 segment 轨迹质量
    MASK_REPAIR = "mask_repair"  # 可恢复的 Mask 批量修复
    MASK_REPAIR_ROLLBACK = "mask_repair_rollback"  # 版本保护的批量回滚
    MASK_FORMAT_IMPORT = "mask_format_import"  # staged receipt 驱动的格式导入
    POINT_CLOUD_CROSS_FRAME = "point_cloud_cross_frame"  # 3D Scene 跨帧传播
    POINT_CLOUD_QUALITY = "point_cloud_quality"  # 3D 点云与 SceneTrack 质量扫描


class AsyncJobStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


# 终态集合（service 层与 retention 都会用到）
ASYNC_JOB_TERMINAL_STATUSES = frozenset(
    {AsyncJobStatus.COMPLETED, AsyncJobStatus.FAILED, AsyncJobStatus.CANCELLED}
)


class AsyncJob(Base):
    __tablename__ = "async_jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    execution_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("prediction_execution_snapshots.id", ondelete="RESTRICT"),
        index=True,
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="SET NULL"),
        nullable=True,
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=AsyncJobStatus.PENDING.value
    )
    progress_pct: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    # 输入摘要（不要塞大对象，仅放 batch_id / 文件数等元数据）
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # 完成后的结果摘要（success_count / failed_count / 路径等）
    result: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    celery_task_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        Index("ix_async_jobs_user_status_created", "user_id", "status", "created_at"),
        Index("ix_async_jobs_project_kind_status", "project_id", "kind", "status"),
        Index("ix_async_jobs_celery_task", "celery_task_id"),
    )
