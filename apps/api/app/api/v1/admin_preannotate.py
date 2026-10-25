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
from sqlalchemy.orm import aliased

from app.db.enums import BatchStatus, UserRole
from app.db.models.ml_backend_pool import MLBackendPoolMember, MLBackendServicePool
from app.db.models.ml_backend_registry import MLBackendRegistry, ProjectMLBackendPool
from app.db.models.project import Project
from app.deps import get_current_user_id, get_db, require_roles
from app.application.bulk_clear_preannotate import (
    bulk_clear_preannotate as run_bulk_clear_preannotate,
)
from app.services.audit import audit_origin_from_request
from app.services.batch_permissions import bulk_preannotation_eligible_condition
from app.services.identity_contracts import IdentityActorFacts
from app.services.workspace_read_relations import project_facts, project_rows
from app.services.labeling_core_read_relations import (
    batch_row,
    failed_prediction_row,
    prediction_row,
    task_row,
)

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
    admin: IdentityActorFacts = Depends(
        require_roles(UserRole.PROJECT_ADMIN, UserRole.SUPER_ADMIN)
    ),
) -> PreannotateQueueResponse:
    """列出已预标 (pre_annotated 状态) 的批次, 按最近一次预标时间倒序.

    含 prediction / failed 计数 + 项目元数据, 供 AIPreAnnotatePage 历史表渲染.
    """
    # 1. 拉所有 pre_annotated 批次 (limit 前)
    stmt = select(batch_row).where(batch_row.status == BatchStatus.PRE_ANNOTATED)
    if admin.role != UserRole.SUPER_ADMIN:
        owner_facts = project_facts()
        stmt = stmt.where(
            batch_row.project_id.in_(
                select(owner_facts.c.id).where(owner_facts.c.owner_id == admin.id)
            )
        )
    bres = await db.execute(stmt.order_by(batch_row.updated_at.desc()).limit(limit))
    batches = list(bres.scalars().all())
    if not batches:
        return PreannotateQueueResponse(items=[])

    batch_ids = [b.id for b in batches]
    project_ids = list({b.project_id for b in batches})

    # 2. project 信息
    projects = aliased(Project, project_rows(), adapt_on_names=True)
    pres = await db.execute(select(projects).where(projects.id.in_(project_ids)))
    projects_by_id = {p.id: p for p in pres.scalars().all()}

    # 3. 每 batch 的 task count
    task_count_q = await db.execute(
        select(task_row.batch_id, func.count(task_row.id))
        .where(task_row.batch_id.in_(batch_ids))
        .group_by(task_row.batch_id)
    )
    task_counts = {bid: cnt for bid, cnt in task_count_q.all()}

    # 4. 每 batch 的 prediction count + max created_at (从 predictions JOIN tasks)
    pred_q = await db.execute(
        select(
            task_row.batch_id,
            func.count(prediction_row.id),
            func.max(prediction_row.created_at),
        )
        .select_from(prediction_row)
        .join(task_row, task_row.id == prediction_row.task_id)
        .where(task_row.batch_id.in_(batch_ids))
        .group_by(task_row.batch_id)
    )
    pred_counts: dict[uuid.UUID, tuple[int, datetime | None]] = {
        bid: (int(cnt), last) for bid, cnt, last in pred_q.all()
    }

    # 5. 每 batch 的 failed count (排除 dismissed)
    fail_q = await db.execute(
        select(task_row.batch_id, func.count(failed_prediction_row.id))
        .select_from(failed_prediction_row)
        .join(task_row, task_row.id == failed_prediction_row.task_id)
        .where(
            task_row.batch_id.in_(batch_ids),
            failed_prediction_row.dismissed_at.is_(None),
        )
        .group_by(task_row.batch_id)
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


@router.post(
    "/admin/preannotate-queue/bulk-clear",
    response_model=BulkClearResponse,
    dependencies=[Depends(require_roles(UserRole.PROJECT_ADMIN, UserRole.SUPER_ADMIN))],
)
async def bulk_clear_preannotate(
    payload: BulkClearRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor_id: uuid.UUID = Depends(get_current_user_id),
) -> BulkClearResponse:
    """v0.9.12 B-16 · 批量清理 /ai-pre 已就绪卡片.

    - predictions_only: 仅清 prediction / failed_prediction / prediction_jobs / prediction_metas;
      batch.status 从 PRE_ANNOTATED 回 ACTIVE (避免 "状态 PRE_ANNOTATED 但 prediction 已空" 矛盾态).
    - reset_to_draft: 复用 BatchService.reset_to_draft, 全量重置 + 级联清理 (Phase 1 落地).

    project_admin 只能处理自己 owned 项目下的 batch; 跨项目越权进 skipped.
    """
    result = await run_bulk_clear_preannotate(
        db,
        batch_ids=payload.batch_ids,
        mode=payload.mode,
        reason=payload.reason,
        actor_id=actor_id,
        origin=audit_origin_from_request(request),
    )
    return BulkClearResponse(**result)


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
    admin: IdentityActorFacts = Depends(
        require_roles(UserRole.PROJECT_ADMIN, UserRole.SUPER_ADMIN)
    ),
) -> PreannotateProjectSummaryResponse:
    """v0.9.12 B-17 · 给 ProjectCardGrid 提供 per-project 聚合.

    仅返回有「已启用」全局 backend 的项目 (EXISTS project_ml_backend_pool WHERE enabled).
    v0.10.38 · 撤回 v0.10.36 的 data_type="image" 过滤: 模态分流改到前端 ProjectDetailPanel
    (image 走文本批量预标, video 走引导卡片+job 历史), summary 返回 data_type 供前端路由.
    """
    project_rows_alias = aliased(Project, project_rows(), adapt_on_names=True)
    stmt = select(project_rows_alias).where(
        select(ProjectMLBackendPool.id)
        .where(
            ProjectMLBackendPool.project_id == project_rows_alias.id,
            ProjectMLBackendPool.enabled.is_(True),
        )
        .exists(),
    )
    if admin.role != UserRole.SUPER_ADMIN:
        stmt = stmt.where(project_rows_alias.owner_id == admin.id)
    pres = await db.execute(stmt)
    projects = list(pres.scalars().all())
    if not projects:
        return PreannotateProjectSummaryResponse(items=[])
    project_ids = [p.id for p in projects]

    bres = await db.execute(
        select(batch_row.project_id, batch_row.status, func.count(batch_row.id))
        .where(batch_row.project_id.in_(project_ids))
        .group_by(batch_row.project_id, batch_row.status)
    )
    batch_counts: dict[tuple[uuid.UUID, str], int] = {
        (pid, status): cnt for pid, status, cnt in bres.all()
    }

    # issue #124 · 卡片「可预标」= active + 未分派人员的 draft，与
    # allows_bulk_preannotation 同源；否则卡片可能显示 0 而详情页仍列有草稿。
    eligible_res = await db.execute(
        select(batch_row.project_id, func.count(batch_row.id))
        .where(
            batch_row.project_id.in_(project_ids),
            bulk_preannotation_eligible_condition(batch_row),
        )
        .group_by(batch_row.project_id)
    )
    eligible_counts = {pid: int(cnt) for pid, cnt in eligible_res.all()}

    # v0.10.49 · prediction_jobs 已收敛进 async_jobs，最近预标时间改查 kind=batch_predict
    from app.services.jobs_domain_query_data import JobsDomainQueryPostgresData

    job_rows = await JobsDomainQueryPostgresData(db).latest_batch_prediction_times(
        project_ids
    )
    last_jobs = {pid: ts for pid, ts in job_rows}

    fail_q = await db.execute(
        select(failed_prediction_row.project_id, func.count(failed_prediction_row.id))
        .where(
            failed_prediction_row.project_id.in_(project_ids),
            failed_prediction_row.dismissed_at.is_(None),
        )
        .group_by(failed_prediction_row.project_id)
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
