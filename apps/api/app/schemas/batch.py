from __future__ import annotations

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from app.schemas.user import UserBrief


class BatchCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: str = ""
    dataset_id: UUID | None = None
    priority: int = Field(50, ge=0, le=100)
    deadline: date | None = None
    # v0.7.2 · 单值分派
    annotator_id: UUID | None = None
    reviewer_id: UUID | None = None


class BatchUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=255)
    description: str | None = None
    priority: int | None = Field(None, ge=0, le=100)
    deadline: date | None = None
    # v0.7.2 · 单值分派
    annotator_id: UUID | None = None
    reviewer_id: UUID | None = None


class BatchOut(BaseModel):
    id: UUID
    project_id: UUID
    dataset_id: UUID | None = None
    display_id: str
    name: str
    description: str = ""
    status: str
    priority: int = 50
    deadline: date | None = None
    assigned_user_ids: list[UUID] = []
    # v0.7.2 · 单值分派字段（一 batch 一标注员 + 一审核员）
    annotator_id: UUID | None = None
    reviewer_id: UUID | None = None
    # v0.7.2 · 责任人可视化 brief（avatar / name / role）
    annotator: UserBrief | None = None
    reviewer: UserBrief | None = None
    total_tasks: int = 0
    completed_tasks: int = 0
    review_tasks: int = 0
    approved_tasks: int = 0
    rejected_tasks: int = 0
    created_by: UUID | None = None
    created_at: datetime
    updated_at: datetime | None = None
    progress_pct: float = 0.0
    review_feedback: str | None = None
    reviewed_at: datetime | None = None
    reviewed_by: UUID | None = None
    # v0.9.15 · ADR-0008 admin-lock
    admin_locked: bool = False
    admin_lock_reason: str | None = None
    admin_locked_at: datetime | None = None
    admin_locked_by: UUID | None = None

    class Config:
        from_attributes = True


class BatchTransition(BaseModel):
    target_status: str
    # v0.7.3：逆向迁移（archived→active / approved→reviewing / rejected→reviewing）必填，1-500 字
    reason: str | None = Field(None, min_length=1, max_length=500)


class BatchReject(BaseModel):
    feedback: str = Field(..., min_length=1, max_length=500)


class BatchSubmitResult(BaseModel):
    """整批送审结果：把批次内所有未送审任务（pending / in_progress）提交质检。"""

    batch_id: UUID
    status: str
    submitted_tasks: int = 0
    skipped_tasks: int = 0
    remaining_tasks: int = 0


class BatchReset(BaseModel):
    """v0.7.6 · 终极重置到 draft。reason 至少 10 字以避免误操作；写入 audit.detail.reason。"""

    reason: str = Field(..., min_length=10, max_length=500)


class ProjectDistributeBatches(BaseModel):
    """v0.7.2 · 项目级 batch 分派：在所选 annotator / reviewer 间圆周分派 batch。
    每个 batch 落到 1 个 annotator + 1 个 reviewer。
    """

    batch_ids: list[UUID] | None = Field(default=None, min_length=1, max_length=500)
    annotator_ids: list[UUID] = []
    reviewer_ids: list[UUID] = []
    # only_unassigned=True：只分派 annotator_id IS NULL（或 reviewer 为空）的 batch；
    # False：覆盖所有 batch
    only_unassigned: bool = True


class ReviewedBatchDistribution(ProjectDistributeBatches):
    preview_version: str = Field(min_length=64, max_length=64)


class BatchDistributeResult(BaseModel):
    distributed_batches: int
    annotator_per_batch: dict[str, str | None] = {}
    reviewer_per_batch: dict[str, str | None] = {}


# v0.7.3 · 多选批量操作
class BulkBatchIds(BaseModel):
    batch_ids: list[UUID] = Field(..., min_length=1, max_length=200)


class BulkBatchReassign(BaseModel):
    batch_ids: list[UUID] = Field(..., min_length=1, max_length=200)
    # 任一可省（None = 不改）；至少传一个
    annotator_id: UUID | None = None
    reviewer_id: UUID | None = None


class BulkBatchActionItem(BaseModel):
    batch_id: UUID
    reason: str


class BulkBatchActionResponse(BaseModel):
    succeeded: list[UUID] = []
    skipped: list[BulkBatchActionItem] = []
    failed: list[BulkBatchActionItem] = []


# v0.9.15 · ADR-0008 admin-lock
class AdminLockRequest(BaseModel):
    reason: str = Field(..., min_length=1, max_length=500)


# v0.9.15 · Bulk approve/reject
class BulkBatchApprove(BaseModel):
    batch_ids: list[UUID] = Field(..., min_length=1, max_length=200)


class BulkBatchReject(BaseModel):
    batch_ids: list[UUID] = Field(..., min_length=1, max_length=200)
    feedback: str = Field(..., min_length=1, max_length=500)


class BatchSplitRequest(BaseModel):
    strategy: Literal["metadata", "id_range", "random", "by_scene"]
    # metadata 策略
    metadata_key: str | None = None
    metadata_value: str | None = None
    # id_range 策略
    item_ids: list[UUID] | None = None
    # random 策略
    n_batches: int | None = Field(None, ge=1, le=100)
    # v0.11.22 · 顺序切分（不打乱）：False 时按 task 创建顺序切分，便于把未归类任务按导入顺序分包。
    # n_batches=1 + shuffle=False 即「把全部未归类任务顺序注入一个新批次」。
    shuffle: bool = True
    # 公共字段
    name_prefix: str = "Batch"
    priority: int = Field(50, ge=0, le=100)
    deadline: date | None = None
    # v0.7.2 · 切批默认分派（每个新切的 batch 都落到同一对人；后续可用项目级 distribute 重新分派）
    annotator_id: UUID | None = None
    reviewer_id: UUID | None = None


class BatchAssignmentPreviewRequest(BaseModel):
    annotator_id: UUID | None
    reviewer_id: UUID | None


class ReviewedBatchAssignment(BatchAssignmentPreviewRequest):
    preview_version: str = Field(min_length=64, max_length=64)
