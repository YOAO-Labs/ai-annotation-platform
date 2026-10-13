"""v0.9.6 · /admin/preannotate-queue 简化版.

返回"AI 预标已就绪 (batch.status='pre_annotated') 的批次列表" + 各自的
prediction_count / failed_count / last_run_at, 让 admin 在 /ai-pre 页面看到
"哪些批次跑完了, 需要去工作台接管 review".

完整 job 历史追踪 (含已结束/已重置批次, 当时 prompt / cost / 耗时) 需要新增
prediction_jobs 表 + worker 写入逻辑, 推迟到 v0.9.7.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import BatchStatus, UserRole
from app.db.models.ml_backend_pool import MLBackendPoolMember, MLBackendServicePool
from app.db.models.ml_backend_registry import MLBackendRegistry, ProjectMLBackendPool
from app.db.models.async_job import AsyncJob
from app.db.models.prediction import FailedPrediction, Prediction
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.db.models.user import User
from app.deps import get_db, require_roles
from app.services.audit import AuditAction, AuditService
from app.services.batch import BatchService
from app.services.batch_permissions import bulk_preannotation_eligible_condition

router = APIRouter()


class PreannotateQueueItem(BaseModel):
    batch_id: uuid.UUID
    batch_name: str
    batch_status: str
    project_id: uuid.UUID
    project_name: str
    project_display_id: str | None = None
    total_tasks: int
    prediction_count: int
    failed_count: int
    last_run_at: datetime | None = None
    can_retry: bool


class PreannotateQueueResponse(BaseModel):
    items: list[PreannotateQueueItem]


@router.get("/admin/preannotate-queue", response_model=PreannotateQueueResponse)
async def list_preannotate_queue(
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_roles(UserRole.PROJECT_ADMIN, UserRole.SUPER_ADMIN)),
) -> PreannotateQueueResponse:
    """列出已预标 (pre_annotated 状态) 的批次, 按最近一次预标时间倒序.

    含 prediction / failed 计数 + 项目元数据, 供 AIPreAnnotatePage 历史表渲染.
    """
    # 1. 拉所有 pre_annotated 批次 (limit 前)
    bres = await db.execute(
        select(TaskBatch)
        .where(TaskBatch.status == BatchStatus.PRE_ANNOTATED)
        .order_by(TaskBatch.updated_at.desc())
        .limit(limit)
    )
    batches = list(bres.scalars().all())
    if not batches:
        return PreannotateQueueResponse(items=[])

    batch_ids = [b.id for b in batches]
    project_ids = list({b.project_id for b in batches})

    # 2. project 信息
    pres = await db.execute(select(Project).where(Project.id.in_(project_ids)))
    projects_by_id = {p.id: p for p in pres.scalars().all()}

    # 3. 每 batch 的 task count
    task_count_q = await db.execute(
        select(Task.batch_id, func.count(Task.id))
        .where(Task.batch_id.in_(batch_ids))
        .group_by(Task.batch_id)
    )
    task_counts = {bid: cnt for bid, cnt in task_count_q.all()}

    # 4. 每 batch 的 prediction count + max created_at (从 predictions JOIN tasks)
    pred_q = await db.execute(
        select(
            Task.batch_id,
            func.count(Prediction.id),
            func.max(Prediction.created_at),
        )
        .select_from(Prediction)
        .join(Task, Task.id == Prediction.task_id)
        .where(Task.batch_id.in_(batch_ids))
        .group_by(Task.batch_id)
    )
    pred_counts: dict[uuid.UUID, tuple[int, datetime | None]] = {
        bid: (int(cnt), last) for bid, cnt, last in pred_q.all()
    }

    # 5. 每 batch 的 failed count (排除 dismissed)
    fail_q = await db.execute(
        select(Task.batch_id, func.count(FailedPrediction.id))
        .select_from(FailedPrediction)
        .join(Task, Task.id == FailedPrediction.task_id)
        .where(
            Task.batch_id.in_(batch_ids),
            FailedPrediction.dismissed_at.is_(None),
        )
        .group_by(Task.batch_id)
    )
    fail_counts = {bid: int(cnt) for bid, cnt in fail_q.all()}

    items: list[PreannotateQueueItem] = []
    for b in batches:
        proj = projects_by_id.get(b.project_id)
        pred_n, last_run = pred_counts.get(b.id, (0, None))
        fail_n = fail_counts.get(b.id, 0)
        items.append(
            PreannotateQueueItem(
                batch_id=b.id,
                batch_name=b.name,
                batch_status=b.status,
                project_id=b.project_id,
                project_name=proj.name if proj else "(已删除项目)",
                project_display_id=getattr(proj, "display_id", None) if proj else None,
                total_tasks=task_counts.get(b.id, 0),
                prediction_count=pred_n,
                failed_count=fail_n,
                last_run_at=last_run,
                can_retry=fail_n > 0,
            )
        )

    return PreannotateQueueResponse(items=items)


# ── v0.9.12 BUG B-16 · 多选批量清理 prediction / 重激活 ────────────────────────


class BulkClearRequest(BaseModel):
    batch_ids: list[uuid.UUID] = Field(..., min_length=1, max_length=50)
    mode: Literal["predictions_only", "reset_to_draft"] = "predictions_only"
    reason: str = Field(..., min_length=10, max_length=500)


class BulkClearItem(BaseModel):
    batch_id: uuid.UUID
    reason: str


class BulkClearResponse(BaseModel):
    succeeded: list[uuid.UUID] = []
    skipped: list[BulkClearItem] = []
    failed: list[BulkClearItem] = []


@router.post("/admin/preannotate-queue/bulk-clear", response_model=BulkClearResponse)
async def bulk_clear_preannotate(
    payload: BulkClearRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_roles(UserRole.PROJECT_ADMIN, UserRole.SUPER_ADMIN)),
) -> BulkClearResponse:
    """v0.9.12 B-16 · 批量清理 /ai-pre 已就绪卡片.

    - predictions_only: 仅清 prediction / failed_prediction / prediction_jobs / prediction_metas;
      batch.status 从 PRE_ANNOTATED 回 ACTIVE (避免 "状态 PRE_ANNOTATED 但 prediction 已空" 矛盾态).
    - reset_to_draft: 复用 BatchService.reset_to_draft, 全量重置 + 级联清理 (Phase 1 落地).

    project_admin 只能处理自己 owned 项目下的 batch; 跨项目越权进 skipped.
    """
    from sqlalchemy import text

    svc = BatchService(db)
    response = BulkClearResponse()
    is_super = admin.role == UserRole.SUPER_ADMIN

    bres = await db.execute(
        select(TaskBatch).where(TaskBatch.id.in_(payload.batch_ids))
    )
    batches_by_id = {b.id: b for b in bres.scalars().all()}

    project_ids = list({b.project_id for b in batches_by_id.values()})
    pres = await db.execute(select(Project).where(Project.id.in_(project_ids)))
    projects_by_id = {p.id: p for p in pres.scalars().all()}

    aggregate_cascade = {
        "predictions": 0,
        "failed_predictions": 0,
        "prediction_jobs": 0,
    }

    for bid in payload.batch_ids:
        batch = batches_by_id.get(bid)
        if not batch:
            response.skipped.append(BulkClearItem(batch_id=bid, reason="not_found"))
            continue
        proj = projects_by_id.get(batch.project_id)
        if not proj:
            response.skipped.append(
                BulkClearItem(batch_id=bid, reason="project_missing")
            )
            continue
        if not is_super and proj.owner_id != admin.id:
            response.skipped.append(BulkClearItem(batch_id=bid, reason="forbidden"))
            continue

        try:
            if payload.mode == "reset_to_draft":
                _, _, cascade = await svc.reset_to_draft(bid, actor_id=admin.id)
            else:
                # predictions_only: 复用 reset_to_draft 的级联 SQL 但保留 task / lock 状态.
                # 实现上先记录 batch 当前 status, 再调 reset_to_draft, 最后状态回 ACTIVE.
                # 简化版: 直接拷贝级联清理 SQL; 不动 task / batch 其他字段.
                from app.db.models.annotation import Annotation
                from app.db.models.prediction import Prediction, FailedPrediction
                from sqlalchemy import delete, update

                task_ids_subq = select(Task.id).where(Task.batch_id == bid)
                await db.execute(
                    update(Annotation)
                    .where(Annotation.task_id.in_(task_ids_subq))
                    .values(parent_prediction_id=None)
                )
                await db.execute(
                    text(
                        "DELETE FROM prediction_metas WHERE prediction_id IN "
                        "(SELECT id FROM predictions WHERE task_id IN "
                        "(SELECT id FROM tasks WHERE batch_id = :bid)) "
                        "OR failed_prediction_id IN "
                        "(SELECT id FROM failed_predictions WHERE task_id IN "
                        "(SELECT id FROM tasks WHERE batch_id = :bid))"
                    ),
                    {"bid": str(bid)},
                )
                pred_r = await db.execute(
                    delete(Prediction).where(Prediction.task_id.in_(task_ids_subq))
                )
                fail_r = await db.execute(
                    delete(FailedPrediction).where(
                        FailedPrediction.task_id.in_(task_ids_subq)
                    )
                )
                # v0.10.49 · prediction_jobs 已收敛进 async_jobs（按 payload.batch_id 清）
                job_r = await db.execute(
                    delete(AsyncJob).where(
                        AsyncJob.kind == "batch_predict",
                        AsyncJob.payload["batch_id"].astext == str(bid),
                    )
                )
                cascade = {
                    "predictions": pred_r.rowcount or 0,
                    "failed_predictions": fail_r.rowcount or 0,
                    "prediction_jobs": job_r.rowcount or 0,
                }
                if batch.status == BatchStatus.PRE_ANNOTATED:
                    batch.status = BatchStatus.ACTIVE

            await db.flush()
            for k in aggregate_cascade:
                aggregate_cascade[k] += cascade.get(k, 0)
            response.succeeded.append(bid)
        except Exception as exc:  # noqa: BLE001
            response.failed.append(
                BulkClearItem(batch_id=bid, reason=f"{type(exc).__name__}: {exc}")
            )

    await AuditService.log(
        db,
        actor=admin,
        action=AuditAction.PREANNOTATE_BULK_CLEAR,
        target_type="batch",
        target_id=str(response.succeeded[0]) if response.succeeded else "none",
        request=request,
        status_code=200,
        detail={
            "mode": payload.mode,
            "reason": payload.reason,
            "requested": len(payload.batch_ids),
            "succeeded_ids": [str(i) for i in response.succeeded],
            "succeeded": len(response.succeeded),
            "skipped": len(response.skipped),
            "failed": len(response.failed),
            "cascade": aggregate_cascade,
        },
    )
    await db.commit()
    return response


# ── v0.9.12 BUG B-17 · /ai-pre 项目卡片网格的聚合数据源 ─────────────────────────


class PreannotateProjectSummary(BaseModel):
    project_id: uuid.UUID
    project_name: str
    project_display_id: str | None = None
    type_key: str
    data_type: str = "image"
    ml_backend_id: uuid.UUID | None = None
    ml_backend_name: str | None = None
    ml_backend_state: str | None = None
    ml_backend_max_concurrency: int | None = None
    ready_batches: int = 0
    # 可预标批次数（active + 未分派标注员与质检员的 draft），与
    # `allows_bulk_preannotation` 同源；ProjectCardGrid 以「可预标」展示。
    active_batches: int = 0
    last_job_at: datetime | None = None
    recent_failures: int = 0


class PreannotateProjectSummaryResponse(BaseModel):
    items: list[PreannotateProjectSummary]


@router.get(
    "/admin/preannotate-summary",
    response_model=PreannotateProjectSummaryResponse,
)
async def list_preannotate_project_summary(
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_roles(UserRole.PROJECT_ADMIN, UserRole.SUPER_ADMIN)),
) -> PreannotateProjectSummaryResponse:
    """v0.9.12 B-17 · 给 ProjectCardGrid 提供 per-project 聚合.

    仅返回有「已启用」全局 backend 的项目 (EXISTS project_ml_backend_pool WHERE enabled).
    v0.10.38 · 撤回 v0.10.36 的 data_type="image" 过滤: 模态分流改到前端 ProjectDetailPanel
    (image 走文本批量预标, video 走引导卡片+job 历史), summary 返回 data_type 供前端路由.
    """
    pres = await db.execute(
        select(Project).where(
            select(ProjectMLBackendPool.id)
            .where(
                ProjectMLBackendPool.project_id == Project.id,
                ProjectMLBackendPool.enabled.is_(True),
            )
            .exists(),
        )
    )
    projects = list(pres.scalars().all())
    if not projects:
        return PreannotateProjectSummaryResponse(items=[])
    project_ids = [p.id for p in projects]

    bres = await db.execute(
        select(TaskBatch.project_id, TaskBatch.status, func.count(TaskBatch.id))
        .where(TaskBatch.project_id.in_(project_ids))
        .group_by(TaskBatch.project_id, TaskBatch.status)
    )
    batch_counts: dict[tuple[uuid.UUID, str], int] = {
        (pid, status): cnt for pid, status, cnt in bres.all()
    }

    # issue #124 · 卡片「可预标」= active + 未分派人员的 draft，与
    # allows_bulk_preannotation 同源；否则卡片可能显示 0 而详情页仍列有草稿。
    eligible_res = await db.execute(
        select(TaskBatch.project_id, func.count(TaskBatch.id))
        .where(
            TaskBatch.project_id.in_(project_ids),
            bulk_preannotation_eligible_condition(),
        )
        .group_by(TaskBatch.project_id)
    )
    eligible_counts = {pid: int(cnt) for pid, cnt in eligible_res.all()}

    # v0.10.49 · prediction_jobs 已收敛进 async_jobs，最近预标时间改查 kind=batch_predict
    job_q = await db.execute(
        select(AsyncJob.project_id, func.max(AsyncJob.created_at))
        .where(
            AsyncJob.kind == "batch_predict",
            AsyncJob.project_id.in_(project_ids),
        )
        .group_by(AsyncJob.project_id)
    )
    last_jobs = {pid: ts for pid, ts in job_q.all()}

    fail_q = await db.execute(
        select(FailedPrediction.project_id, func.count(FailedPrediction.id))
        .where(
            FailedPrediction.project_id.in_(project_ids),
            FailedPrediction.dismissed_at.is_(None),
        )
        .group_by(FailedPrediction.project_id)
    )
    fail_counts = {pid: int(cnt) for pid, cnt in fail_q.all()}

    # v0.19.0 ADR-0044 · 按项目启用关联 join 全局注册表取本项目可用 backend。
    # v0.23.3 ADR-0050 · 经服务池层 (project_ml_backend_pool → pool member → registry)。
    bk_q = await db.execute(
        select(ProjectMLBackendPool.project_id, MLBackendRegistry)
        .join(
            MLBackendPoolMember,
            MLBackendPoolMember.pool_id == ProjectMLBackendPool.pool_id,
        )
        .join(
            MLBackendRegistry,
            MLBackendRegistry.id == MLBackendPoolMember.registry_id,
        )
        .where(
            ProjectMLBackendPool.project_id.in_(project_ids),
            ProjectMLBackendPool.enabled.is_(True),
        )
    )
    rows = list(bk_q.all())
    backends_by_project: dict[uuid.UUID, MLBackendRegistry] = {}
    # 同项目多 backend 时优先选项目主池的 legacy instance; 否则任取一条.
    # v0.23.3: 项目主绑定存 pool id, 经 MLBackendServicePool.legacy_instance_id 解析。
    pool_ids = {proj.ml_backend_pool_id for proj in projects if proj.ml_backend_pool_id}
    legacy_map: dict[uuid.UUID, uuid.UUID] = {}
    if pool_ids:
        legacy_q = await db.execute(
            select(
                MLBackendServicePool.id, MLBackendServicePool.legacy_instance_id
            ).where(MLBackendServicePool.id.in_(pool_ids))
        )
        legacy_map = {pid: leg for pid, leg in legacy_q.all() if leg is not None}
    for pid, bk in rows:
        backends_by_project.setdefault(pid, bk)
    for proj in projects:
        preferred_registry = (
            legacy_map.get(proj.ml_backend_pool_id) if proj.ml_backend_pool_id else None
        )
        if preferred_registry:
            for pid, bk in rows:
                if bk.id == preferred_registry:
                    backends_by_project[proj.id] = bk
                    break

    items: list[PreannotateProjectSummary] = []
    for proj in projects:
        bk = backends_by_project.get(proj.id)
        max_cc = None
        if bk and bk.extra_params:
            mc = bk.extra_params.get("max_concurrency")
            if isinstance(mc, (int, float)):
                max_cc = int(mc)
        items.append(
            PreannotateProjectSummary(
                project_id=proj.id,
                project_name=proj.name,
                project_display_id=proj.display_id,
                type_key=proj.type_key,
                data_type=proj.data_type,
                ml_backend_id=bk.id if bk else None,
                ml_backend_name=bk.name if bk else None,
                ml_backend_state=bk.state if bk else None,
                ml_backend_max_concurrency=max_cc,
                ready_batches=batch_counts.get((proj.id, BatchStatus.PRE_ANNOTATED), 0),
                active_batches=eligible_counts.get(proj.id, 0),
                last_job_at=last_jobs.get(proj.id),
                recent_failures=fail_counts.get(proj.id, 0),
            )
        )

    items.sort(
        key=lambda x: (
            x.last_job_at.timestamp() if x.last_job_at else 0,
            x.project_name,
        ),
        reverse=True,
    )
    return PreannotateProjectSummaryResponse(items=items)
