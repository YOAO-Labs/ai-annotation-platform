import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import inspect
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.quality_job_dispatch import QualityJobDispatcher
from app.deps import (
    get_quality_job_dispatcher,
    get_db,
    get_current_user,
    get_current_user_id,
    project_access_for_path,
    require_project_visible,
    require_project_owner,
    require_project_capability,
)
from app.db.enums import BatchStatus
from app.db.models.project import Project
from app.schemas.batch import (
    BatchCreate,
    BatchAssignmentPreviewRequest,
    ReviewedBatchAssignment,
    BatchUpdate,
    BatchOut,
    BatchTransition,
    BatchReject,
    BatchSubmitResult,
    BatchReset,
    BatchSplitRequest,
    ProjectDistributeBatches,
    BatchDistributeResult,
    BulkBatchIds,
    BulkBatchReassign,
    BulkBatchActionResponse,
    AdminLockRequest,
    BulkBatchApprove,
    BulkBatchReject,
)
from app.schemas.management import BatchDistributionPreview
from app.schemas.batch import ReviewedBatchDistribution
from app.schemas.export import (
    ExportRequestBody,
    LidarExportPreflightRequest,
    LidarExportPreflightResponse,
)
from app.services.batch import (
    BatchService,
    assert_can_transition,
    REVERSE_TRANSITIONS,
)
from aap_workspace_contract.access import ProjectAccess, ProjectCapability
from app.services.management import preview_batch_distribution
from app.services.audit import AuditAction, AuditService, audit_origin_from_request
from app.services.notification import NotificationService
from app.services.user_brief import resolve_briefs_with_project_role
from app.services.audit_read_relations import audit_event
from sqlalchemy import select as sa_select

from app.application.task_submission import TaskSubmissionWorkflow
from app.application.task_workflow_errors import TaskWorkflowError
from app.services.identity_contracts import IdentityActorFacts

from app.services.labeling_core_read_relations import batch_row, get_core_row, task_row

router = APIRouter()


def _batch_to_out(batch, briefs: dict | None = None) -> BatchOut:
    total = batch.total_tasks or 1
    pct = round((batch.completed_tasks / total) * 100, 1) if batch.total_tasks else 0.0
    out = BatchOut.model_validate(batch)
    out.progress_pct = pct
    if briefs is not None:
        # v0.7.2：单值语义 — 直接按 annotator_id / reviewer_id 拿 brief
        if batch.annotator_id is not None:
            out.annotator = briefs.get(str(batch.annotator_id))
        if batch.reviewer_id is not None:
            out.reviewer = briefs.get(str(batch.reviewer_id))
    return out


async def _refresh_batch(db: AsyncSession, batch):
    state = inspect(batch)
    if state.identity is None:
        raise InvalidRequestError(
            f"Could not refresh instance of {type(batch).__name__}"
        )
    batch_id = state.identity[0]
    db.expire(batch)
    refreshed = await get_core_row(db, batch_row, batch_id, populate_existing=True)
    if refreshed is None:
        raise InvalidRequestError(
            f"Could not refresh {type(batch).__name__} with identity {batch_id}"
        )
    return refreshed


async def _briefs_for_batches(db, project_id, batches) -> dict:
    """v0.7.2：一次 IN 查询解析所有批次的 annotator_id + reviewer_id → UserBrief。"""
    all_ids = set()
    for b in batches:
        if b.annotator_id is not None:
            all_ids.add(str(b.annotator_id))
        if b.reviewer_id is not None:
            all_ids.add(str(b.reviewer_id))
    if not all_ids:
        return {}
    return await resolve_briefs_with_project_role(db, project_id, all_ids)


@router.get("")
async def list_batches(
    project_id: uuid.UUID,
    status: str | None = None,
    project: Project = Depends(require_project_visible),
    db: AsyncSession = Depends(get_db),
):
    svc = BatchService(db)
    batches = await svc.list_by_project(project_id, status)
    briefs = await _briefs_for_batches(db, project_id, batches)
    return [_batch_to_out(b, briefs) for b in batches]


@router.get("/unclassified-count")
async def get_unclassified_task_count(
    project_id: uuid.UUID,
    project: Project = Depends(require_project_visible),
    db: AsyncSession = Depends(get_db),
):
    """v0.7.3：项目下未归类（batch_id IS NULL）任务数。
    给 BatchesSection 顶部横带「未归类 N 条 · 去分包」用。
    """
    from sqlalchemy import select as sa_sel, func as sa_func

    n = (
        await db.execute(
            sa_sel(sa_func.count())
            .select_from(task_row)
            .where(
                task_row.project_id == project_id,
                task_row.batch_id.is_(None),
            )
        )
    ).scalar() or 0
    return {"count": int(n)}


@router.get("/{batch_id}")
async def get_batch(
    project_id: uuid.UUID,
    batch_id: uuid.UUID,
    project: Project = Depends(require_project_visible),
    db: AsyncSession = Depends(get_db),
):
    svc = BatchService(db)
    batch = await svc.get(batch_id)
    if not batch or batch.project_id != project_id:
        raise HTTPException(status_code=404, detail="Batch not found")
    briefs = await _briefs_for_batches(db, project_id, [batch])
    return _batch_to_out(batch, briefs)


@router.post("", status_code=201)
async def create_batch(
    project_id: uuid.UUID,
    data: BatchCreate,
    request: Request,
    project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(get_current_user),
):
    svc = BatchService(db)
    batch = await svc.create(project_id, data, current_user.id)
    await AuditService.log(
        db,
        actor=current_user,
        action=AuditAction.BATCH_CREATED,
        target_type="batch",
        target_id=str(batch.id),
        request=request,
        status_code=201,
        detail={"name": batch.name, "project_id": str(project_id)},
    )
    await db.commit()
    batch = await _refresh_batch(db, batch)
    briefs = await _briefs_for_batches(db, project_id, [batch])
    return _batch_to_out(batch, briefs)


@router.patch("/{batch_id}")
async def update_batch(
    project_id: uuid.UUID,
    batch_id: uuid.UUID,
    data: BatchUpdate,
    project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
):
    svc = BatchService(db)
    batch = await svc.get(batch_id)
    if not batch or batch.project_id != project_id:
        raise HTTPException(status_code=404, detail="Batch not found")
    batch = await svc.update(batch_id, data, project_id=project_id)
    await db.commit()
    batch = await _refresh_batch(db, batch)
    briefs = await _briefs_for_batches(db, project_id, [batch])
    return _batch_to_out(batch, briefs)


async def _assignment_plan(db, project_id, batch_id, data, *, lock=False):
    svc = BatchService(db)
    return await preview_batch_distribution(
        db,
        project_id=project_id,
        annotator_ids=[data.annotator_id] if data.annotator_id else [],
        reviewer_ids=[data.reviewer_id] if data.reviewer_id else [],
        only_unassigned=False,
        single_batch_id=batch_id,
        clear_unselected=True,
        validate_targets=svc.lock_and_validate_assignment_targets,
        lock_batches=lock,
    )


@router.post("/{batch_id}/assignment-preview", response_model=BatchDistributionPreview)
async def preview_batch_assignment(
    project_id: uuid.UUID,
    batch_id: uuid.UUID,
    data: BatchAssignmentPreviewRequest,
    project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
):
    return await _assignment_plan(db, project_id, batch_id, data)


@router.post("/{batch_id}/assignment-apply", response_model=BatchOut)
async def apply_batch_assignment(
    project_id: uuid.UUID,
    batch_id: uuid.UUID,
    data: ReviewedBatchAssignment,
    request: Request,
    project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
    actor: IdentityActorFacts = Depends(get_current_user),
):
    plan = await _assignment_plan(db, project_id, batch_id, data, lock=True)
    if plan.preview_version != data.preview_version:
        raise HTTPException(
            status_code=409, detail="批次或成员负载已变化，请重新预览分派"
        )
    batch = await BatchService(db).update(
        batch_id,
        BatchUpdate(annotator_id=data.annotator_id, reviewer_id=data.reviewer_id),
        project_id=project_id,
    )
    await AuditService.log(
        db,
        actor=actor,
        action="batch.assignment_apply",
        target_type="batch",
        target_id=str(batch_id),
        request=request,
        status_code=200,
        detail={"assignment": plan.items[0].model_dump(mode="json")},
    )
    await db.commit()
    batch = await _refresh_batch(db, batch)
    return _batch_to_out(batch, await _briefs_for_batches(db, project_id, [batch]))


@router.delete("/{batch_id}", status_code=204)
async def delete_batch(
    project_id: uuid.UUID,
    batch_id: uuid.UUID,
    request: Request,
    force: bool = False,
    project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(get_current_user),
):
    svc = BatchService(db)
    batch = await svc.get(batch_id)
    if not batch or batch.project_id != project_id:
        raise HTTPException(status_code=404, detail="Batch not found")
    affected = batch.total_tasks
    # v0.11.25：含进行中成果/已预标时 svc.delete 会抛 409 requires_force；force=true 走强制清理删除
    await svc.delete(batch_id, force=force, actor_id=current_user.id)
    await AuditService.log(
        db,
        actor=current_user,
        action=AuditAction.BATCH_DELETED,
        target_type="batch",
        target_id=str(batch_id),
        request=request,
        status_code=204,
        detail={
            "project_id": str(project_id),
            "name": batch.name,
            "affected_tasks": affected,
            "forced": force,
        },
    )
    await db.commit()


@router.post("/{batch_id}/transition")
async def transition_batch(
    project_id: uuid.UUID,
    batch_id: uuid.UUID,
    data: BatchTransition,
    request: Request,
    project: Project = Depends(require_project_visible),
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(get_current_user),
    access: ProjectAccess = Depends(project_access_for_path),
):
    svc = BatchService(db)
    batch = await svc.get(batch_id)
    if not batch or batch.project_id != project_id:
        raise HTTPException(status_code=404, detail="Batch not found")

    # v0.7.0：按 (from, to) 校验角色（403 携带可读 detail）
    assert_can_transition(
        current_user,
        project,
        batch,
        data.target_status,
        project_role=access.project_role,
    )

    # v0.7.3：逆向迁移强制 reason（schema 层面 reason 是可选，这里按方向决定是否必填）
    is_reverse = (batch.status, data.target_status) in REVERSE_TRANSITIONS
    if is_reverse and not data.reason:
        raise HTTPException(
            status_code=400, detail="reason is required for reverse transition"
        )

    old_status = batch.status
    batch = await svc.transition(batch_id, data.target_status, current_user.id)
    audit_detail: dict = {"before": old_status, "after": batch.status}
    if is_reverse:
        audit_detail["reverse"] = True
        audit_detail["reason"] = data.reason
    await AuditService.log(
        db,
        actor=current_user,
        action=AuditAction.BATCH_STATUS_CHANGED,
        target_type="batch",
        target_id=str(batch_id),
        request=request,
        status_code=200,
        detail=audit_detail,
    )

    # v0.7.3：逆向迁移按方向分发通知
    notif_recipients: list[uuid.UUID] = []
    notif_type: str | None = None
    if (old_status, batch.status) == (BatchStatus.ARCHIVED, BatchStatus.ACTIVE):
        notif_type = "batch.unarchived"
        if batch.annotator_id is not None:
            notif_recipients.append(batch.annotator_id)
        if batch.reviewer_id is not None and batch.reviewer_id != batch.annotator_id:
            notif_recipients.append(batch.reviewer_id)
    elif (old_status, batch.status) in {
        (BatchStatus.APPROVED, BatchStatus.REVIEWING),
        (BatchStatus.REJECTED, BatchStatus.REVIEWING),
    }:
        notif_type = "batch.review_reopened"
        if batch.reviewer_id is not None and batch.reviewer_id != current_user.id:
            notif_recipients.append(batch.reviewer_id)

    pending_notifications = []
    if notif_type and notif_recipients:
        notif_svc = NotificationService(db)
        pending_notifications = await notif_svc.notify_many(
            user_ids=notif_recipients,
            type=notif_type,
            target_type="batch",
            target_id=batch.id,
            payload={
                "batch_display_id": batch.display_id,
                "batch_name": batch.name,
                "project_id": str(project_id),
                "from_status": old_status,
                "reason": data.reason,
            },
        )

    await db.commit()
    if pending_notifications:
        await NotificationService(db).publish_committed(pending_notifications)
    batch = await _refresh_batch(db, batch)
    briefs = await _briefs_for_batches(db, project_id, [batch])
    return _batch_to_out(batch, briefs)


@router.post("/{batch_id}/submit-review", response_model=BatchSubmitResult)
async def submit_batch_review(
    project_id: uuid.UUID,
    batch_id: uuid.UUID,
    request: Request,
    project: Project = Depends(require_project_visible),
    db: AsyncSession = Depends(get_db),
    actor_id: uuid.UUID = Depends(get_current_user_id),
    access: ProjectAccess = Depends(project_access_for_path),
    quality_dispatcher: QualityJobDispatcher = Depends(get_quality_job_dispatcher),
):
    """整批送审：把批次内所有未送审任务（pending / in_progress）提交质检。

    与单任务提交共用 `TaskSubmissionWorkflow`，因此审核轮次、任务锁释放、Mask QC
    自动运行与 contributor 快照语义完全一致；结束后由 `check_auto_transitions`
    把批次推进到 reviewing。`rejected` 任务需先 accept-rejection 重做，不在此
    批量集合内；remaining_tasks 只统计仍为 pending / in_progress 的任务。

    只接受 `annotating` / `reviewing` 两种来源状态（与 Web 入口一致），并按固定
    分块处理/提交，避免大批次一次性物化全部 task 行锁与事务。
    """
    try:
        result = await TaskSubmissionWorkflow(db).submit_batch_review(
            project_id,
            batch_id,
            actor_id=actor_id,
            access=access,
            origin=audit_origin_from_request(request),
            quality_dispatcher=quality_dispatcher,
        )
    except TaskWorkflowError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return BatchSubmitResult(**result)


@router.post("/split")
async def split_batches(
    project_id: uuid.UUID,
    data: BatchSplitRequest,
    request: Request,
    project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(get_current_user),
):
    svc = BatchService(db)
    batches = await svc.split(project_id, data, current_user.id)
    for b in batches:
        await AuditService.log(
            db,
            actor=current_user,
            action=AuditAction.BATCH_CREATED,
            target_type="batch",
            target_id=str(b.id),
            request=request,
            status_code=200,
            detail={
                "name": b.name,
                "strategy": data.strategy,
                "total_tasks": b.total_tasks,
            },
        )
    await db.commit()
    for b in batches:
        b = await _refresh_batch(db, b)
    briefs = await _briefs_for_batches(db, project_id, batches)
    return [_batch_to_out(b, briefs) for b in batches]


@router.post("/distribute-batches", response_model=BatchDistributeResult)
async def distribute_batches_in_project(
    project_id: uuid.UUID,
    data: ProjectDistributeBatches,
    request: Request,
    project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(get_current_user),
):
    """v0.7.2：项目级 batch 分派 — 把项目下未分派/全部 batch 圆周分配给所选 annotator/reviewer。
    每 batch 落到 1 个 annotator + 1 个 reviewer；同步回填该 batch 下所有 task 的 assignee_id/reviewer_id。
    """
    svc = BatchService(db)
    summary = await svc.distribute_batches_in_project(
        project_id,
        annotator_ids=data.annotator_ids,
        reviewer_ids=data.reviewer_ids,
        only_unassigned=data.only_unassigned,
        batch_ids=data.batch_ids,
    )
    await AuditService.log(
        db,
        actor=current_user,
        action=AuditAction.BATCH_DISTRIBUTE_EVEN,
        target_type="project",
        target_id=str(project_id),
        request=request,
        status_code=200,
        detail={
            "scope": "project_batches",
            "distributed_batches": summary["distributed_batches"],
            "annotator_count": len(data.annotator_ids),
            "reviewer_count": len(data.reviewer_ids),
            "only_unassigned": data.only_unassigned,
        },
    )
    await db.commit()
    return summary


@router.post("/distribution-preview", response_model=BatchDistributionPreview)
async def preview_distribution_in_project(
    project_id: uuid.UUID,
    data: ProjectDistributeBatches,
    project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(get_current_user),
):
    """Preview assignment changes without mutating batches or task rows."""

    del project, current_user
    svc = BatchService(db)
    return await preview_batch_distribution(
        db,
        project_id=project_id,
        annotator_ids=data.annotator_ids,
        reviewer_ids=data.reviewer_ids,
        only_unassigned=data.only_unassigned,
        batch_ids=data.batch_ids,
        validate_targets=svc.lock_and_validate_assignment_targets,
    )


@router.post("/distribution-apply", response_model=BatchDistributeResult)
async def apply_distribution_in_project(
    project_id: uuid.UUID,
    data: ReviewedBatchDistribution,
    request: Request,
    project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(get_current_user),
):
    """Apply a previously reviewed project distribution plan."""

    del project
    svc = BatchService(db)
    summary = await svc.distribute_batches_in_project(
        project_id,
        annotator_ids=data.annotator_ids,
        reviewer_ids=data.reviewer_ids,
        only_unassigned=data.only_unassigned,
        batch_ids=data.batch_ids,
        preview_version=data.preview_version,
    )
    await AuditService.log(
        db,
        actor=current_user,
        action=AuditAction.BATCH_DISTRIBUTE_EVEN,
        target_type="project",
        target_id=str(project_id),
        request=request,
        status_code=200,
        detail={
            "scope": "project_batches_management_apply",
            "distributed_batches": summary["distributed_batches"],
            "annotator_count": len(data.annotator_ids),
            "reviewer_count": len(data.reviewer_ids),
            "only_unassigned": data.only_unassigned,
        },
    )
    await db.commit()
    return summary


@router.post("/{batch_id}/reject")
async def reject_batch(
    project_id: uuid.UUID,
    batch_id: uuid.UUID,
    data: BatchReject,
    request: Request,
    project: Project = Depends(require_project_visible),
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(get_current_user),
    access: ProjectAccess = Depends(
        require_project_capability(ProjectCapability.REVIEW_WRITE.value)
    ),
):
    svc = BatchService(db)
    batch = await svc.get(batch_id)
    if not batch or batch.project_id != project_id:
        raise HTTPException(status_code=404, detail="Batch not found")

    # v0.7.0：复用 transition 鉴权矩阵（reviewing → rejected 的角色门）
    assert_can_transition(
        current_user,
        project,
        batch,
        "rejected",
        project_role=access.project_role,
    )

    batch, affected = await svc.reject_batch(
        batch_id,
        feedback=data.feedback,
        reviewer_id=current_user.id,
        actor_id=current_user.id,
    )
    await AuditService.log(
        db,
        actor=current_user,
        action=AuditAction.BATCH_REJECTED,
        target_type="batch",
        target_id=str(batch_id),
        request=request,
        status_code=200,
        detail={"affected_tasks": affected, "feedback": data.feedback},
    )

    # v0.7.2 · 单值语义：只通知该批次的标注员（reviewer 是 actor 本人无需通知）
    pending_notifications = []
    if batch.annotator_id is not None:
        notif_svc = NotificationService(db)
        pending_notifications = await notif_svc.notify_many(
            user_ids=[batch.annotator_id],
            type="batch.rejected",
            target_type="batch",
            target_id=batch.id,
            payload={
                "batch_display_id": batch.display_id,
                "batch_name": batch.name,
                "project_id": str(project_id),
                "feedback": data.feedback,
                "affected_tasks": affected,
            },
        )

    await db.commit()
    if pending_notifications:
        await NotificationService(db).publish_committed(pending_notifications)
    batch = await _refresh_batch(db, batch)
    briefs = await _briefs_for_batches(db, project_id, [batch])
    return _batch_to_out(batch, briefs)


# ── v0.7.6 · Reset → draft 终极重置 ───────────────────────────────────────


@router.post("/{batch_id}/reset", response_model=BatchOut)
async def reset_batch_to_draft(
    project_id: uuid.UUID,
    batch_id: uuid.UUID,
    data: BatchReset,
    request: Request,
    project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(get_current_user),
):
    """v0.7.6 · owner 兜底：把任意状态批次回退到 draft。

    - task 全部回 pending（保留 annotation 记录与 is_active）
    - 删除 task_locks 释放标注员锁
    - 清 review_feedback / reviewed_at / reviewed_by
    - 强制 reason ≥ 10 字（schema 层校验），写入 audit.detail.reason
    """
    svc = BatchService(db)
    batch = await svc.get(batch_id)
    if not batch or batch.project_id != project_id:
        raise HTTPException(status_code=404, detail="Batch not found")

    from_status = batch.status
    batch, affected, cascade = await svc.reset_to_draft(
        batch_id, actor_id=current_user.id
    )

    await AuditService.log(
        db,
        actor=current_user,
        action=AuditAction.BATCH_RESET_TO_DRAFT,
        target_type="batch",
        target_id=str(batch_id),
        request=request,
        status_code=200,
        detail={
            "from_status": from_status,
            "reason": data.reason,
            "affected_tasks": affected,
            "cascade": cascade,
        },
    )

    await db.commit()
    batch = await _refresh_batch(db, batch)
    briefs = await _briefs_for_batches(db, project_id, [batch])
    return _batch_to_out(batch, briefs)


# ── v0.9.15 · ADR-0008 Admin Lock/Unlock ─────────────────────────────────


@router.post("/{batch_id}/admin-lock", response_model=BatchOut)
async def admin_lock_batch(
    project_id: uuid.UUID,
    batch_id: uuid.UUID,
    data: AdminLockRequest,
    request: Request,
    project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(get_current_user),
):
    svc = BatchService(db)
    batch = await svc.get(batch_id)
    if not batch or batch.project_id != project_id:
        raise HTTPException(status_code=404, detail="Batch not found")

    batch = await svc.admin_lock(
        batch_id, reason=data.reason, locked_by=current_user.id
    )

    await AuditService.log(
        db,
        actor=current_user,
        action=AuditAction.BATCH_ADMIN_LOCK,
        target_type="batch",
        target_id=str(batch_id),
        request=request,
        status_code=200,
        detail={"reason": data.reason, "batch_status": batch.status},
    )

    notif_recipients: list[uuid.UUID] = []
    if batch.annotator_id and batch.annotator_id != current_user.id:
        notif_recipients.append(batch.annotator_id)
    if batch.reviewer_id and batch.reviewer_id != current_user.id:
        notif_recipients.append(batch.reviewer_id)
    if project.owner_id != current_user.id:
        notif_recipients.append(project.owner_id)
    pending_notifications = []
    if notif_recipients:
        notif_svc = NotificationService(db)
        pending_notifications = await notif_svc.notify_many(
            user_ids=notif_recipients,
            type="batch.admin_locked",
            target_type="batch",
            target_id=batch.id,
            payload={
                "batch_display_id": batch.display_id,
                "batch_name": batch.name,
                "project_id": str(project_id),
                "reason": data.reason,
            },
        )

    await db.commit()
    if pending_notifications:
        await NotificationService(db).publish_committed(pending_notifications)
    batch = await _refresh_batch(db, batch)
    briefs = await _briefs_for_batches(db, project_id, [batch])
    return _batch_to_out(batch, briefs)


@router.post("/{batch_id}/admin-unlock", response_model=BatchOut)
async def admin_unlock_batch(
    project_id: uuid.UUID,
    batch_id: uuid.UUID,
    request: Request,
    project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(get_current_user),
):
    svc = BatchService(db)
    batch = await svc.get(batch_id)
    if not batch or batch.project_id != project_id:
        raise HTTPException(status_code=404, detail="Batch not found")

    batch = await svc.admin_unlock(batch_id)

    await AuditService.log(
        db,
        actor=current_user,
        action=AuditAction.BATCH_ADMIN_UNLOCK,
        target_type="batch",
        target_id=str(batch_id),
        request=request,
        status_code=200,
        detail={"batch_status": batch.status},
    )

    notif_recipients: list[uuid.UUID] = []
    if batch.annotator_id and batch.annotator_id != current_user.id:
        notif_recipients.append(batch.annotator_id)
    if batch.reviewer_id and batch.reviewer_id != current_user.id:
        notif_recipients.append(batch.reviewer_id)
    pending_notifications = []
    if notif_recipients:
        notif_svc = NotificationService(db)
        pending_notifications = await notif_svc.notify_many(
            user_ids=notif_recipients,
            type="batch.admin_unlocked",
            target_type="batch",
            target_id=batch.id,
            payload={
                "batch_display_id": batch.display_id,
                "batch_name": batch.name,
                "project_id": str(project_id),
            },
        )

    await db.commit()
    if pending_notifications:
        await NotificationService(db).publish_committed(pending_notifications)
    batch = await _refresh_batch(db, batch)
    briefs = await _briefs_for_batches(db, project_id, [batch])
    return _batch_to_out(batch, briefs)


# ── v0.7.3 · 多选批量操作 ─────────────────────────────────────────────────


def _bulk_audit_detail(payload: dict, summary: dict) -> dict:
    return {
        "batch_ids": [str(x) for x in payload.get("batch_ids", [])],
        "succeeded": [str(x) for x in summary["succeeded"]],
        "skipped": [
            {"batch_id": str(x["batch_id"]), "reason": x["reason"]}
            for x in summary["skipped"]
        ],
        "failed": [
            {"batch_id": str(x["batch_id"]), "reason": x["reason"]}
            for x in summary["failed"]
        ],
    }


@router.post("/bulk-archive", response_model=BulkBatchActionResponse)
async def bulk_archive_batches(
    project_id: uuid.UUID,
    data: BulkBatchIds,
    request: Request,
    project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(get_current_user),
):
    svc = BatchService(db)
    summary = await svc.bulk_archive(project_id, data.batch_ids)
    await AuditService.log(
        db,
        actor=current_user,
        action=AuditAction.BULK_BATCH_ARCHIVE,
        target_type="project",
        target_id=str(project_id),
        request=request,
        status_code=200,
        detail=_bulk_audit_detail({"batch_ids": data.batch_ids}, summary),
    )
    await db.commit()
    return summary


@router.post("/bulk-delete", response_model=BulkBatchActionResponse)
async def bulk_delete_batches(
    project_id: uuid.UUID,
    data: BulkBatchIds,
    request: Request,
    force: bool = False,
    project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(get_current_user),
):
    svc = BatchService(db)
    # v0.11.25：force=false 时含进行中成果/已预标的批次进 failed(reason=requires_force)
    summary = await svc.bulk_delete(
        project_id, data.batch_ids, force=force, actor_id=current_user.id
    )
    await AuditService.log(
        db,
        actor=current_user,
        action=AuditAction.BULK_BATCH_DELETE,
        target_type="project",
        target_id=str(project_id),
        request=request,
        status_code=200,
        detail={
            **_bulk_audit_detail({"batch_ids": data.batch_ids}, summary),
            "forced": force,
        },
    )
    await db.commit()
    return summary


@router.post("/bulk-reassign", response_model=BulkBatchActionResponse)
async def bulk_reassign_batches(
    project_id: uuid.UUID,
    data: BulkBatchReassign,
    request: Request,
    project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(get_current_user),
):
    raw = data.model_dump(exclude_unset=True)
    annotator_set = "annotator_id" in raw
    reviewer_set = "reviewer_id" in raw
    svc = BatchService(db)
    summary = await svc.bulk_reassign(
        project_id,
        data.batch_ids,
        annotator_id=data.annotator_id,
        reviewer_id=data.reviewer_id,
        annotator_set=annotator_set,
        reviewer_set=reviewer_set,
    )
    audit_detail = _bulk_audit_detail({"batch_ids": data.batch_ids}, summary)
    if annotator_set:
        audit_detail["annotator_id"] = (
            str(data.annotator_id) if data.annotator_id else None
        )
    if reviewer_set:
        audit_detail["reviewer_id"] = (
            str(data.reviewer_id) if data.reviewer_id else None
        )
    await AuditService.log(
        db,
        actor=current_user,
        action=AuditAction.BULK_BATCH_REASSIGN,
        target_type="project",
        target_id=str(project_id),
        request=request,
        status_code=200,
        detail=audit_detail,
    )
    await db.commit()
    return summary


@router.post("/bulk-activate", response_model=BulkBatchActionResponse)
async def bulk_activate_batches(
    project_id: uuid.UUID,
    data: BulkBatchIds,
    request: Request,
    project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(get_current_user),
):
    svc = BatchService(db)
    summary = await svc.bulk_activate(project_id, data.batch_ids)
    await AuditService.log(
        db,
        actor=current_user,
        action=AuditAction.BULK_BATCH_ACTIVATE,
        target_type="project",
        target_id=str(project_id),
        request=request,
        status_code=200,
        detail=_bulk_audit_detail({"batch_ids": data.batch_ids}, summary),
    )
    await db.commit()
    return summary


@router.post("/bulk-approve", response_model=BulkBatchActionResponse)
async def bulk_approve_batches(
    project_id: uuid.UUID,
    data: BulkBatchApprove,
    request: Request,
    project: Project = Depends(require_project_visible),
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(get_current_user),
    access: ProjectAccess = Depends(
        require_project_capability(ProjectCapability.REVIEW_WRITE.value)
    ),
):
    svc = BatchService(db)
    summary = await svc.bulk_approve(
        project_id, data.batch_ids, actor_id=current_user.id
    )
    await AuditService.log(
        db,
        actor=current_user,
        action=AuditAction.BULK_BATCH_APPROVE,
        target_type="project",
        target_id=str(project_id),
        request=request,
        status_code=200,
        detail=_bulk_audit_detail({"batch_ids": data.batch_ids}, summary),
    )
    await db.commit()
    return summary


@router.post("/bulk-reject", response_model=BulkBatchActionResponse)
async def bulk_reject_batches(
    project_id: uuid.UUID,
    data: BulkBatchReject,
    request: Request,
    project: Project = Depends(require_project_visible),
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(get_current_user),
    access: ProjectAccess = Depends(
        require_project_capability(ProjectCapability.REVIEW_WRITE.value)
    ),
):
    svc = BatchService(db)
    summary = await svc.bulk_reject(
        project_id,
        data.batch_ids,
        feedback=data.feedback,
        reviewer_id=current_user.id,
        actor_id=current_user.id,
    )
    audit_detail = _bulk_audit_detail({"batch_ids": data.batch_ids}, summary)
    audit_detail["feedback"] = data.feedback
    await AuditService.log(
        db,
        actor=current_user,
        action=AuditAction.BULK_BATCH_REJECT,
        target_type="project",
        target_id=str(project_id),
        request=request,
        status_code=200,
        detail=audit_detail,
    )
    # 通知各批次的标注员
    pending_notifications = []
    if summary["succeeded"]:
        loaded = await svc._list_batches_in_project(project_id, summary["succeeded"])
        notif_svc = NotificationService(db)
        for batch in loaded.values():
            if batch.annotator_id and batch.annotator_id != current_user.id:
                pending_notifications.extend(
                    await notif_svc.notify_many(
                        user_ids=[batch.annotator_id],
                        type="batch.rejected",
                        target_type="batch",
                        target_id=batch.id,
                        payload={
                            "batch_display_id": batch.display_id,
                            "batch_name": batch.name,
                            "project_id": str(project_id),
                            "feedback": data.feedback,
                            "bulk": True,
                        },
                    )
                )
    await db.commit()
    if pending_notifications:
        await NotificationService(db).publish_committed(pending_notifications)
    return summary


@router.get("/{batch_id}/audit-logs")
async def list_batch_audit_logs(
    project_id: uuid.UUID,
    batch_id: uuid.UUID,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    project: Project = Depends(require_project_visible),
    db: AsyncSession = Depends(get_db),
):
    """v0.7.3：批次操作历史抽屉数据源。
    返回该批次相关的 audit_log，按时间倒序，包含：
    - 直接 target=batch 的事件（创建/状态迁移/驳回/删除）
    - bulk 类操作中提及到该批次的项目级事件（detail_json.batch_ids 包含此 id）
    """
    from sqlalchemy import or_

    batch = await BatchService(db).get(batch_id)
    if batch is not None and batch.project_id != project_id:
        raise HTTPException(status_code=404, detail="Batch not found")

    bulk_actions = (
        AuditAction.BULK_BATCH_ARCHIVE.value,
        AuditAction.BULK_BATCH_DELETE.value,
        AuditAction.BULK_BATCH_REASSIGN.value,
        AuditAction.BULK_BATCH_ACTIVATE.value,
    )
    direct = (audit_event.target_type == "batch") & (
        audit_event.target_id == str(batch_id)
    )
    if batch is None:
        path = f"/api/v1/projects/{project_id}/batches/{batch_id}"
        direct &= or_(
            audit_event.detail_json["project_id"].astext == str(project_id),
            audit_event.path == path,
            audit_event.path.startswith(path + "/", autoescape=True),
        )
    bulk_match = (
        (audit_event.target_type == "project")
        & (audit_event.target_id == str(project_id))
        & (audit_event.action.in_(bulk_actions))
        & (audit_event.detail_json.contains({"batch_ids": [str(batch_id)]}))
    )
    rows = (
        (
            await db.execute(
                sa_select(audit_event)
                .where(or_(direct, bulk_match))
                .order_by(audit_event.created_at.desc(), audit_event.id.desc())
                .offset(offset)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )

    return [
        {
            "id": r.id,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "actor_id": str(r.actor_id) if r.actor_id else None,
            "actor_email": r.actor_email,
            "actor_role": r.actor_role,
            "action": r.action,
            "target_type": r.target_type,
            "target_id": r.target_id,
            "detail": r.detail_json,
        }
        for r in rows
    ]


@router.post(
    "/{batch_id}/exports/lidar:preflight",
    response_model=LidarExportPreflightResponse,
)
async def preflight_batch_lidar_export(
    project_id: uuid.UUID,
    batch_id: uuid.UUID,
    body: LidarExportPreflightRequest,
    project: Project = Depends(require_project_visible),
    db: AsyncSession = Depends(get_db),
    _access: ProjectAccess = Depends(
        require_project_capability(ProjectCapability.EXPORT_ANNOTATIONS.value)
    ),
) -> LidarExportPreflightResponse:
    if project.data_type != "lidar":
        raise HTTPException(status_code=422, detail="project is not a lidar project")
    batch = await BatchService(db).get(batch_id)
    if not batch or batch.project_id != project_id:
        raise HTTPException(status_code=404, detail="Batch not found")
    from app.services.exporting.packaging import clean_export_targets

    try:
        targets = clean_export_targets(body.targets, project.data_type)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    from app.services.exporting.lidar_preflight import preflight_lidar_export

    return await preflight_lidar_export(
        db,
        project_id=project_id,
        batch_id=batch_id,
        targets=targets,
        options=body.lidar,
    )


@router.post("/{batch_id}/export", status_code=202)
async def export_batch(
    request: Request,
    project_id: uuid.UUID,
    batch_id: uuid.UUID,
    body: ExportRequestBody | None = None,
    targets: list[str] = Query(
        default=["coco"],
        description="导出目标，可多选：coco / yolo-det / yolo-obb / yolo-seg / aap_json"
        " / video_json / yolo-frames-det / yolo-frames-seg / coco-frames-seg / davis"
        " / youtube-vos / mots / mot / kitti"
        " / coco-multicamera / nuscenes / pointmask"
        "（voc 仅可单选，走同步下载；lidar.kitti 为 3D label，video.kitti 为 tracking label）",
    ),
    include_attributes: bool = Query(True),
    video_frame_mode: str = Query(
        "keyframes",
        pattern="^(keyframes|all_frames)$",
        description="video-track 导出帧模式：keyframes 仅关键帧，all_frames 展开逐帧插值；图片项目忽略",
    ),
    axis_frame: str = Query(
        "iso",
        pattern="^(iso|source)$",
        description="3D box export axis frame: iso keeps platform-normalized PSR; source maps back to dataset axis convention",
    ),
    video_overlap_policy: str = Query(
        "error",
        pattern="^(error|z_order|larger_area|smaller_area)$",
    ),
    mots_frame_base: int = Query(0, ge=0, le=1),
    project: Project = Depends(require_project_visible),
    actor: IdentityActorFacts = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _access: ProjectAccess = Depends(
        require_project_capability(ProjectCapability.EXPORT_ANNOTATIONS.value)
    ),
):
    # v0.10.27 · 导出异步化：创建 async_job(kind=export) + 派发 run_export，返回 {job_id}。
    # v0.10.43 · 多目标（方案 B）。VOC 仅可单选，走同步 blob；不删避免破坏 API 契约。
    from app.services.audit import AuditService, AuditAction, export_detail
    from app.services.exporting.packaging import clean_export_targets

    try:
        targets = clean_export_targets(targets, project.data_type)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    svc_batch = BatchService(db)
    batch = await svc_batch.get(batch_id)
    if not batch or batch.project_id != project_id:
        raise HTTPException(status_code=404, detail="Batch not found")

    lidar_options = body.lidar if body else None
    lidar_options_payload = (
        lidar_options.model_dump(mode="json", exclude_none=True)
        if lidar_options
        else None
    )
    if project.data_type == "lidar":
        from app.services.exporting.lidar_preflight import (
            LidarExportPreflightFailed,
            assert_lidar_export_ready,
        )

        try:
            await assert_lidar_export_ready(
                db,
                project_id=project_id,
                batch_id=batch_id,
                targets=targets,
                options=lidar_options,
            )
        except LidarExportPreflightFailed as exc:
            raise HTTPException(
                status_code=409,
                detail=exc.report.model_dump(mode="json"),
            ) from exc

    from app.services.exporting.video_scope import normalize_video_export_scope

    try:
        video_scope = await normalize_video_export_scope(
            db,
            actor_id=actor.id,
            project=project,
            request=body.scope if body else None,
            batch_id=batch_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    video_scope_payload = video_scope.as_dict() if video_scope else None

    fname = f"{project.display_id}_{batch.display_id}"

    if "voc" in targets:
        if targets != ["voc"]:
            raise HTTPException(
                status_code=400, detail="voc 格式只能单独导出，不能与其它目标混选"
            )
        from app.services.exporting.service import ExportService, UnsupportedExportError

        svc = ExportService(db)
        try:
            data = await svc.export_voc(
                project_id, batch_id=batch_id, include_attributes=include_attributes
            )
        except UnsupportedExportError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await AuditService.log(
            db,
            actor=actor,
            action=AuditAction.BATCH_EXPORT,
            target_type="batch",
            target_id=str(batch_id),
            request=request,
            status_code=200,
            detail={
                "format": "voc",
                "project_id": str(project_id),
                "batch_display_id": batch.display_id,
            },
        )
        await db.commit()
        return Response(
            content=data,
            media_type="application/zip",
            headers={"Content-Disposition": f"attachment; filename={fname}_voc.zip"},
        )

    from app.services import async_job as async_job_svc
    from app.db.models.async_job import AsyncJobKind
    from app.services.jobs_queue import dispatch_job
    from app.services.jobs_dispatch import (
        prepare_job_dispatch,
        publish_prepared_dispatch,
    )

    from app.services.exporting.reference_media import capture_export_authority

    from app.services.exporting import output_authority

    output_proof = await output_authority.capture(db, project, actor, request)
    export_opts = {
        "include_attributes": include_attributes,
        "video_frame_mode": video_frame_mode,
        "axis_frame": axis_frame,
        "video_overlap_policy": video_overlap_policy,
        "mots_frame_base": mots_frame_base,
        **({"video_export_scope": video_scope_payload} if video_scope_payload else {}),
        **({"lidar": lidar_options_payload} if lidar_options_payload else {}),
    }
    job = await async_job_svc.create_job(
        db,
        kind=AsyncJobKind.EXPORT.value,
        user_id=actor.id,
        project_id=project.id,
        payload={
            "output_authority": output_proof,
            "batch_id": str(batch_id),
            "targets": targets,
            "project_id": str(project_id),
            "project_display_id": project.display_id,
            "batch_display_id": batch.display_id,
            **export_opts,
        },
    )
    from app.services.content_snapshot import ContentError
    from app.services.exporting.media_jobs import capture_job
    from app.services.video_canonical import VideoBoundaryUnreconciledError

    try:
        media_bundle = await capture_job(
            db, job, project, batch_id=batch_id, targets=targets, opts=export_opts
        )
    except VideoBoundaryUnreconciledError as exc:
        raise HTTPException(409, detail=exc.detail) from None
    except ContentError as exc:
        raise HTTPException(status_code=exc.status, detail={"code": exc.code}) from exc
    export_authority = await capture_export_authority(
        db,
        project,
        actor,
        request,
        batch_id=batch_id,
        frozen_snapshot=media_bundle.input_snapshot if media_bundle else None,
    )
    if export_authority:
        job.payload = {**job.payload, "export_authority": export_authority}
    await AuditService.log(
        db,
        actor=actor,
        action=AuditAction.BATCH_EXPORT,
        target_type="batch",
        target_id=str(batch_id),
        request=request,
        status_code=202,
        detail=export_detail(
            actor=actor,
            request=request,
            base={
                "targets": targets,
                "project_id": str(project_id),
                "batch_display_id": batch.display_id,
            },
            filter_criteria=export_opts,
        ),
    )
    execution_id = str(uuid.uuid4())
    dispatch_args = []
    dispatch_kwargs = {
        "project_id": str(project_id),
        "batch_id": str(batch_id),
        "targets": targets,
        "opts": {
            "include_attributes": include_attributes,
            "video_frame_mode": video_frame_mode,
            "axis_frame": axis_frame,
            "video_overlap_policy": video_overlap_policy,
            "mots_frame_base": mots_frame_base,
            **(
                {"video_export_scope": video_scope_payload}
                if video_scope_payload
                else {}
            ),
            **({"lidar": lidar_options_payload} if lidar_options_payload else {}),
        },
        "async_job_id": str(job.id),
    }
    lease = await prepare_job_dispatch(
        db,
        job_id=job.id,
        task_name="app.workers.export.run_export",
        execution_id=execution_id,
        args=dispatch_args,
        kwargs=dispatch_kwargs,
    )
    await db.commit()

    await publish_prepared_dispatch(
        db,
        lease,
        publish=lambda: dispatch_job(
            task_name="app.workers.export.run_export",
            execution_id=execution_id,
            args=dispatch_args,
            kwargs=dispatch_kwargs,
        ),
    )
    return {"job_id": str(job.id)}
