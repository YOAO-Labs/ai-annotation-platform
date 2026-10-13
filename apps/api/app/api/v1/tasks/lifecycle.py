import uuid
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import (
    get_db,
    get_current_user,
)
from app.db.models.user import User
from app.services.annotation_evidence import (
    clear_review_contributor_evidence,
    freeze_review_contributor_evidence,
)
from app.services.audit import AuditAction, AuditService
from app.services.project_access import ProjectAccess
from app.services.task_lock import TaskLockService


from app.api.v1.tasks._shared import (
    _load_task_or_404,
    _assert_task_visible,
    _assert_effective_task_assignee,
    _effective_task_assignee_id,
    _assert_task_editable,
    _start_review_round,
    _ensure_review_round,
    _capture_first_review_contributor_snapshot,
    _task_contributor_snapshot,
    _submission_assignment_start,
    perform_task_submit,
    require_task_annotation_write_lifecycle,
)

router = APIRouter()


@router.post("/{task_id}/submit")
async def submit_task(
    task_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    access: ProjectAccess = Depends(require_task_annotation_write_lifecycle),
):
    from app.db.models.task import Task

    task = (
        await db.execute(
            select(Task)
            .where(Task.id == task_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    await _assert_task_visible(db, task, current_user, access=access)
    _assert_effective_task_assignee(
        current_user,
        await _effective_task_assignee_id(db, task),
        action="submit",
        project_id=task.project_id,
        allow_open_pool=True,
        access=access,
    )
    if task.status not in ("pending", "in_progress"):
        raise HTTPException(
            status_code=409,
            detail={"reason": "task_not_submittable", "status": task.status},
        )

    _assert_task_editable(task, current_user, access=access)

    now = datetime.now(timezone.utc)
    result = await perform_task_submit(db, task, actor=current_user, now=now)

    from app.services.batch import BatchService

    batch_svc = BatchService(db)
    await batch_svc.check_auto_transitions(task.batch_id)
    if task.batch_id:
        await batch_svc.recalculate_counters(task.batch_id)

    mask_qc_run = result["mask_qc_run"]
    mask_qc_job = result["mask_qc_job"]
    await AuditService.log(
        db,
        actor=current_user,
        action=AuditAction.TASK_SUBMIT,
        target_type="task",
        target_id=str(task_id),
        request=request,
        status_code=200,
        detail={
            "project_id": str(task.project_id),
            "assignee_id": str(task.assignee_id) if task.assignee_id else None,
            "contributor_ids": result["contributor_ids"],
            "review_contributor_ids": result["review_contributor_ids"],
            "review_submitter_id": (
                str(result["review_submitter_id"])
                if result["review_submitter_id"]
                else None
            ),
            "review_round_id": str(result["review_round_id"]),
            "result": "submitted",
            "mask_qc_run_id": str(mask_qc_run.id) if mask_qc_run else None,
            "mask_qc_status": result["mask_qc_status"],
        },
    )

    await db.commit()
    mask_qc_status = result["mask_qc_status"] if result["has_mask"] else None
    if (
        result["mask_qc_created"]
        and mask_qc_run is not None
        and mask_qc_job is not None
    ):
        from app.services.mask_qc.service import MaskQCError, dispatch_mask_qc_run

        try:
            await dispatch_mask_qc_run(
                db,
                run_id=mask_qc_run.id,
                async_job_id=mask_qc_job.id,
            )
        except MaskQCError:
            mask_qc_status = "failed"
    return {
        "status": "submitted",
        "task_id": str(task_id),
        "mask_qc_run_id": str(mask_qc_run.id) if mask_qc_run else None,
        "mask_qc_status": mask_qc_status,
    }


_VALID_SKIP_REASONS = {"image_corrupt", "no_target", "unclear", "other"}


class SkipTaskRequest(BaseModel):
    reason: str
    note: str | None = None


@router.post("/{task_id}/skip")
async def skip_task(
    task_id: uuid.UUID,
    body: SkipTaskRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    access: ProjectAccess = Depends(require_task_annotation_write_lifecycle),
):
    """v0.8.7 F7 · 标注员跳过任务并附原因，自动转 reviewer 复核。

    状态机：
      - pending / in_progress → review（与 submit 行为一致，但不要求有标注）
      - 其他状态 → 409
    业务约束：
      - reason ∈ {image_corrupt, no_target, unclear, other}；其他 422
      - reason="other" 时建议带 note，但 note 可空（前端兜底）
    """
    if body.reason not in _VALID_SKIP_REASONS:
        raise HTTPException(
            status_code=422,
            detail={"reason": "invalid_skip_reason", "value": body.reason},
        )

    # The router holds the actor User lock before this Task lock. Refresh the
    # assignment under the Task lock before checking visibility and ownership.
    from app.db.models.task import Task

    task = (
        await db.execute(
            select(Task)
            .where(Task.id == task_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    await _assert_task_visible(db, task, current_user, access=access)
    _assert_effective_task_assignee(
        current_user,
        await _effective_task_assignee_id(db, task),
        action="skip",
        project_id=task.project_id,
        allow_open_pool=True,
        access=access,
    )
    if task.status not in ("pending", "in_progress"):
        raise HTTPException(
            status_code=409,
            detail={"reason": "task_not_skippable", "status": task.status},
        )

    _assert_task_editable(task, current_user, access=access)

    now = datetime.now(timezone.utc)
    # A2 · capture the inherited batch assignee before the legacy path replaces
    # an empty assignee with the actual actor; the frozen evidence must include
    # both the effective assignee and the submitter.
    inherited_assignee_id = await _effective_task_assignee_id(db, task)
    if task.assignee_id is None:
        task.assignee_id = current_user.id
        task.assigned_at = await _submission_assignment_start(
            db, task_id, current_user.id, now
        )

    review_round_id = _start_review_round(task)
    task.status = "review"
    task.skip_reason = body.reason
    task.skipped_at = now
    task.submitted_at = now
    # 清空上一轮 review 痕迹
    task.reviewer_id = None
    task.reviewer_is_override = False
    task.reviewer_claimed_at = None
    task.reviewed_at = None
    task.reject_reason = None
    task.reject_reason_type = None

    lock_svc = TaskLockService(db)
    await lock_svc.release(task_id, current_user.id)

    from app.services.batch import BatchService

    batch_svc = BatchService(db)
    await batch_svc.check_auto_transitions(task.batch_id)
    if task.batch_id:
        await batch_svc.recalculate_counters(task.batch_id)

    contributor_ids = await _task_contributor_snapshot(db, task)
    _capture_first_review_contributor_snapshot(task, contributor_ids)
    freeze_contributor_ids = list(contributor_ids)
    if (
        inherited_assignee_id is not None
        and str(inherited_assignee_id) not in freeze_contributor_ids
    ):
        freeze_contributor_ids.append(str(inherited_assignee_id))
    freeze_review_contributor_evidence(
        task, submitter_id=current_user.id, contributor_ids=freeze_contributor_ids
    )
    await AuditService.log(
        db,
        actor=current_user,
        action=AuditAction.TASK_SKIP,
        target_type="task",
        target_id=str(task_id),
        request=request,
        status_code=200,
        detail={
            "project_id": str(task.project_id),
            "skip_reason": body.reason,
            "note": body.note,
            "assignee_id": str(task.assignee_id) if task.assignee_id else None,
            "contributor_ids": contributor_ids,
            "review_contributor_ids": task.review_contributor_ids,
            "review_submitter_id": (
                str(task.review_submitter_id) if task.review_submitter_id else None
            ),
            "review_round_id": str(review_round_id),
            "result": "skipped",
        },
    )
    await db.commit()
    return {
        "status": "skipped",
        "task_id": str(task_id),
        "skip_reason": body.reason,
    }


@router.post("/{task_id}/withdraw")
async def withdraw_task(
    task_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    access: ProjectAccess = Depends(require_task_annotation_write_lifecycle),
):
    """v0.6.5: 标注员撤回质检提交。
    前提：status=review、assignee == 当前用户、reviewer_claimed_at IS NULL。
    审核员一旦 claim 就锁死撤回入口，避免与审核动作打架。"""
    task = await _load_task_or_404(db, task_id)
    from app.db.models.project import Project

    project = await db.get(Project, task.project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Task not found")
    _assert_effective_task_assignee(
        current_user,
        await _effective_task_assignee_id(db, task),
        action="withdraw",
        project_id=task.project_id,
        access=access,
    )
    if task.status != "review":
        raise HTTPException(
            status_code=409,
            detail={"reason": "task_not_in_review", "status": task.status},
        )
    if task.reviewer_claimed_at is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "task_already_claimed",
                "reviewer_id": str(task.reviewer_id) if task.reviewer_id else None,
            },
        )

    task.status = "in_progress"
    task.submitted_at = None
    # A2 · leaving review invalidates the round's frozen evidence; the
    # annotation contributor accumulator is retained conservatively.
    clear_review_contributor_evidence(task)

    if project:
        project.review_tasks = max((project.review_tasks or 0) - 1, 0)

    from app.services.batch import BatchService

    batch_svc = BatchService(db)
    await batch_svc.check_auto_transitions(task.batch_id)
    if task.batch_id:
        await batch_svc.recalculate_counters(task.batch_id)

    await AuditService.log(
        db,
        actor=current_user,
        action=AuditAction.TASK_WITHDRAW,
        target_type="task",
        target_id=str(task_id),
        request=request,
        status_code=200,
        detail={"project_id": str(task.project_id)},
    )

    await db.commit()
    return {"status": "withdrawn", "task_id": str(task_id)}


# ── Review endpoints ───────────────────���────────────────────────────────────


@router.post("/{task_id}/reopen")
async def reopen_task(
    task_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    access: ProjectAccess = Depends(require_task_annotation_write_lifecycle),
):
    """v0.6.5: 标注员对已通过任务单方面重开编辑。
    前提：status=completed 且 assignee == 当前用户（admin 兜底）。
    清空 reviewer_* 但 detail 留 original_reviewer_id 用于通知；
    annotations 原地保留可继续改，依赖 audit_logs 回溯历史。"""
    task = await _load_task_or_404(db, task_id)
    from app.db.models.project import Project

    project = await db.get(Project, task.project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Task not found")
    _assert_effective_task_assignee(
        current_user,
        await _effective_task_assignee_id(db, task),
        action="reopen",
        project_id=task.project_id,
        access=access,
    )
    if task.status != "completed":
        raise HTTPException(
            status_code=409,
            detail={"reason": "task_not_completed", "status": task.status},
        )
    original_reviewer_id = task.reviewer_id
    task.status = "in_progress"
    task.reopened_count = (task.reopened_count or 0) + 1
    task.last_reopened_at = datetime.now(timezone.utc)
    task.reviewer_id = None
    task.reviewer_is_override = False
    task.reviewer_claimed_at = None
    task.reviewed_at = None
    task.reject_reason = None
    task.reject_reason_type = None
    task.submitted_at = None
    clear_review_contributor_evidence(task)

    if project:
        project.completed_tasks = max((project.completed_tasks or 0) - 1, 0)

    from app.services.batch import BatchService

    batch_svc = BatchService(db)
    await batch_svc.check_auto_transitions(task.batch_id)
    if task.batch_id:
        await batch_svc.recalculate_counters(task.batch_id)

    await AuditService.log(
        db,
        actor=current_user,
        action=AuditAction.TASK_REOPEN,
        target_type="task",
        target_id=str(task_id),
        request=request,
        status_code=200,
        detail={
            "project_id": str(task.project_id),
            "original_reviewer_id": str(original_reviewer_id)
            if original_reviewer_id
            else None,
            "contributor_ids": await _task_contributor_snapshot(db, task),
            "review_round_id": str(_ensure_review_round(task)),
            "reopened_count": task.reopened_count,
        },
    )

    # v0.7.6 · 通知中心 fan-out：原 reviewer 收到 task.reopened
    # Deferred until after the commit below; see NotificationService.notify.
    pending_notifications = []
    if original_reviewer_id is not None:
        from app.services.notification import NotificationService

        notif_svc = NotificationService(db)
        pending_notifications = await notif_svc.notify_many(
            user_ids=[original_reviewer_id],
            type="task.reopened",
            target_type="task",
            target_id=task.id,
            payload={
                "task_display_id": task.display_id,
                "project_id": str(task.project_id),
                "actor_id": str(current_user.id),
                "actor_name": current_user.name,
                "reopened_count": task.reopened_count,
            },
        )

    await db.commit()
    if pending_notifications:
        await NotificationService(db).publish_committed(pending_notifications)
    return {
        "status": "reopened",
        "task_id": str(task_id),
        "reopened_count": task.reopened_count,
    }


@router.post("/{task_id}/accept-rejection")
async def accept_rejection(
    task_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    access: ProjectAccess = Depends(require_task_annotation_write_lifecycle),
):
    """M1 · 标注员接受退回，将 task 从 rejected 转回 in_progress 开始重做。
    不清空 reject_reason（保留审核员退回原因，前端可降级为"重做中"提示）。"""
    task = await _load_task_or_404(db, task_id)
    from app.db.models.project import Project

    project = await db.get(Project, task.project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Task not found")
    _assert_effective_task_assignee(
        current_user,
        await _effective_task_assignee_id(db, task),
        action="accept rejection",
        project_id=task.project_id,
        access=access,
    )
    if task.status != "rejected":
        raise HTTPException(
            status_code=409,
            detail={"reason": "task_not_rejected", "status": task.status},
        )
    task.status = "in_progress"
    clear_review_contributor_evidence(task)

    from app.services.batch import BatchService

    batch_svc = BatchService(db)
    await batch_svc.check_auto_transitions(task.batch_id)
    if task.batch_id:
        await batch_svc.recalculate_counters(task.batch_id)

    await AuditService.log(
        db,
        actor=current_user,
        action=AuditAction.TASK_ACCEPT_REJECTION,
        target_type="task",
        target_id=str(task_id),
        request=request,
        status_code=200,
        detail={
            "project_id": str(task.project_id),
            "reject_reason": task.reject_reason,
            "review_round_id": str(_ensure_review_round(task)),
            "contributor_ids": await _task_contributor_snapshot(db, task),
        },
    )

    await db.commit()
    return {"status": "in_progress", "task_id": str(task_id)}


# ── Task Lock endpoints ─────────────────────────────────────────────────────
