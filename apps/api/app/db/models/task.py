import enum
import uuid
from datetime import datetime
from sqlalchemy import (
    String,
    Boolean,
    Integer,
    Float,
    DateTime,
    ForeignKey,
    func,
    Index,
    text,
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base


class RejectReasonType(str, enum.Enum):
    """v0.10.16 · reviewer 驳回结构化枚举。收紧到 4 类，前端 4 个预设按钮一一映射。"""

    MISSING = "missing"  # 漏标
    EXTRA = "extra"  # 多标
    WRONG_LABEL = "wrong_label"  # 类别错误
    WRONG_GEOMETRY = "wrong_geometry"  # 位置/尺寸不准


class Task(Base):
    __tablename__ = "tasks"

    # v0.11.30 · 大表查询地基索引（与迁移 0090 同步，经 10 万行 EXPLAIN 实测保留）。
    # 单列索引仍由各列的 index=True 声明；此处补两条热路径所需的组合。
    __table_args__ = (
        Index("ix_tasks_project_created_id", "project_id", "created_at", "id"),
        Index(
            "ix_tasks_batch_unlabeled",
            "batch_id",
            postgresql_where=text("is_labeled = false"),
        ),
        # v0.12.0 · 未归类池 cursor 分页（与迁移 0091 同步）。
        Index(
            "ix_tasks_project_unbatched",
            "project_id",
            "created_at",
            "id",
            postgresql_where=text("batch_id IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), index=True
    )
    dataset_item_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("dataset_items.id"), index=True
    )
    display_id: Mapped[str] = mapped_column(String(30), nullable=False, unique=True)
    file_name: Mapped[str] = mapped_column(String(500), nullable=False)
    file_path: Mapped[str] = mapped_column(String(1000), nullable=False)
    file_type: Mapped[str] = mapped_column(String(20), default="image")
    tags: Mapped[list] = mapped_column(JSONB, default=list)
    status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    assignee_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id")
    )
    # Batch assignment also populates assignee_id. Preserve the intent of a
    # selected-task assignment even when it equals the current batch default.
    assignee_is_override: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # v0.8.4 · 分派时间戳（效率看板「平均单题耗时」分母 = submitted_at - assigned_at）
    assigned_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    is_labeled: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    overlap: Mapped[int] = mapped_column(Integer, default=1)
    total_annotations: Mapped[int] = mapped_column(Integer, default=0)
    total_predictions: Mapped[int] = mapped_column(Integer, default=0)
    precomputed_agreement: Mapped[float | None] = mapped_column(Float)
    sequence_order: Mapped[int | None] = mapped_column(Integer)
    thumbnail_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    blurhash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    batch_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("task_batches.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    # v0.6.5 · 状态机锁定相关
    submitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reviewer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    reviewer_is_override: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    reviewer_claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Stable identifier for the current submission/review round.  It lets
    # performance reporting join a review decision to the submission that
    # created it even after the mutable reviewer fields are cleared.
    review_round_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    # First-review facts are written once for tasks created after the
    # attribution rollout. The contributor snapshot is captured at the first
    # submit/skip and finalized with the first decision. NULL eligibility keeps
    # pre-rollout history explicitly unknown rather than allowing an
    # audit-retention gap to look like a clean first pass.
    first_review_eligible: Mapped[bool | None] = mapped_column(
        Boolean, nullable=True, default=True, server_default="true"
    )
    first_reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    first_review_result: Mapped[str | None] = mapped_column(String(16), nullable=True)
    first_review_contributor_ids: Mapped[list | None] = mapped_column(
        JSONB, nullable=True
    )
    # 项目级员工角色 · 增量 A/B：可空贡献者证据。NULL = 未知/legacy；数据库列无
    # server default，历史行保持未知。ORM/Core 新建行用 Python-only default=list 落
    # 已知空集合（旧二进制不写此列 → NULL）。annotation 累积与 review 冻结逻辑见
    # services/annotation_evidence.py。
    annotation_contributor_ids: Mapped[list | None] = mapped_column(
        JSONB, nullable=True, default=list
    )
    review_contributor_ids: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    review_submitter_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reject_reason: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    reject_reason_type: Mapped[str | None] = mapped_column(
        String(20), nullable=True, index=True
    )
    # v0.8.7 F7 · 任务跳过：标注员遇图像损坏/无目标/不清晰时直转 reviewer 复核
    skip_reason: Mapped[str | None] = mapped_column(String(50), nullable=True)
    skipped_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reopened_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_reopened_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
