import uuid
from datetime import datetime, timezone
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import (
    get_db,
    get_current_user,
)
from app.db.models.user import User
from app.db.models.project import Project
from app.db.models.notification import Notification
from app.db.models.task_batch import TaskBatch
from app.schemas.task import (
    ReviewClaimResponse,
)
from app.services.annotation_evidence import (
    assert_review_evidence_current,
    clear_review_contributor_evidence,
)
from app.services.audit import AuditAction, AuditService


from app.api.v1.tasks._shared import (
    _assert_access_binding,
    _assert_task_visible,
    _effective_task_assignee_id,
    _ensure_review_round,
    _record_first_review_fact,
    _review_round_contributor_snapshot,
    _task_contributor_snapshot,
    require_task_review_write,
)
from app.services.project_access import ProjectAccess
from app.services.scheduler import (
    effective_task_reviewer_id,
)

router = APIRouter()


class ReviewAction(BaseModel):
    """v0.10.16 · reviewer 驳回 payload。`reason_type` 自 v0.10.16 起必填，
    `reason` 仍为可空自由文本补充。"""

    reason_type: Literal["missing", "extra", "wrong_label", "wrong_geometry"] | None = (
        None
    )
    reason: str | None = None


class ApproveTaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_qc_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    warning_issue_ids: list[uuid.UUID] = Field(default_factory=list, max_length=500)
    note: str | None = Field(default=None, max_length=2000)


async def _lock_review_task(
    db: AsyncSession, task_id: uuid.UUID, user: User, access: ProjectAccess
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
    await _assert_task_visible(db, task, user, access=access)
    return task


async def _assert_review_evidence_allowed(
    db: AsyncSession, *, task, user: User
) -> None:
    """Block self-review and unknown-evidence decisions before privileged returns.

    Called for every review claim/decision so a manager cannot review work they
    contributed to, a revoked round cannot be completed, and legacy tasks whose
    contributor evidence is unknown stay blocked (409).  Historical performance
    attribution keeps using ``_review_round_contributor_snapshot``.
    """

    effective_annotator_id = await _effective_task_assignee_id(db, task)
    assert_review_evidence_current(
        task, user.id, effective_annotator_id=effective_annotator_id
    )


async def _assert_review_owner(
    db: AsyncSession, *, task, user: User, access: ProjectAccess
) -> Project:
    project = await db.get(Project, task.project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    _assert_access_binding(access, user=user, project_id=task.project_id)
    if access.is_manager:
        return project
    batch = await db.get(TaskBatch, task.batch_id) if task.batch_id else None
    effective_reviewer_id = effective_task_reviewer_id(task, batch)
    if effective_reviewer_id != user.id or task.reviewer_claimed_at is None:
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "task_review_not_claimed_by_user",
                "reviewer_id": (
                    str(effective_reviewer_id) if effective_reviewer_id else None
                ),
            },
        )
    return project


@router.post("/{task_id}/review/claim", response_model=ReviewClaimResponse)
async def claim_review(
    task_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    access: ProjectAccess = Depends(require_task_review_write),
):
    """v0.6.5: 审核员进入审核页时调用（幂等）。
    第一个调用者写 reviewer_id + reviewer_claimed_at；
    后续调用者读取已存在的认领信息（不覆盖）。
    `reviewer_claimed_at` 一经设置即冻结标注员的 withdraw 入口。"""
    task = await _lock_review_task(db, task_id, current_user, access)
    if task.status != "review":
        raise HTTPException(
            status_code=409,
            detail={"reason": "task_not_in_review", "status": task.status},
        )
    await _assert_review_evidence_allowed(db, task=task, user=current_user)

    if task.reviewer_claimed_at is None:
        batch = await db.get(TaskBatch, task.batch_id) if task.batch_id else None
        effective_reviewer_id = effective_task_reviewer_id(task, batch)
        if (
            effective_reviewer_id is not None
            and effective_reviewer_id != current_user.id
            and not access.is_manager
        ):
            raise HTTPException(
                status_code=409,
                detail={
                    "reason": "task_review_assigned_to_other",
                    "reviewer_id": str(effective_reviewer_id),
                },
            )
        task.reviewer_id = current_user.id
        task.reviewer_claimed_at = datetime.now(timezone.utc)
        await AuditService.log(
            db,
            actor=current_user,
            action=AuditAction.TASK_REVIEW_CLAIM,
            target_type="task",
            target_id=str(task_id),
            request=request,
            status_code=200,
            detail={
                "project_id": str(task.project_id),
                "review_round_id": str(_ensure_review_round(task)),
                "assignee_id": str(task.assignee_id) if task.assignee_id else None,
                "contributor_ids": await _task_contributor_snapshot(db, task),
            },
        )
        await db.commit()

    return ReviewClaimResponse(
        task_id=task.id,
        reviewer_id=task.reviewer_id,
        reviewer_claimed_at=task.reviewer_claimed_at,
        is_self=(task.reviewer_id == current_user.id),
    )


@router.post("/{task_id}/review/approve")
async def approve_task(
    task_id: uuid.UUID,
    request: Request,
    body: ApproveTaskRequest | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    access: ProjectAccess = Depends(require_task_review_write),
):
    task = await _lock_review_task(db, task_id, current_user, access)
    if task.status != "review":
        raise HTTPException(
            status_code=409,
            detail={"reason": "task_not_in_review", "status": task.status},
        )

    await _assert_review_evidence_allowed(db, task=task, user=current_user)
    project = await _assert_review_owner(
        db, task=task, user=current_user, access=access
    )
    from app.services.video_canonical import (
        VideoBoundaryUnreconciledError,
        assert_task_boundaries_reconciled,
    )

    try:
        await assert_task_boundaries_reconciled(db, project=project, task=task)
    except VideoBoundaryUnreconciledError as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from exc
    from app.services.mask_qc.config import load_mask_qc_config
    from app.services.mask_qc.service import (
        current_completed_task_run,
        current_open_task_issues,
        qc_digest_for_issues,
    )

    config = load_mask_qc_config(project.mask_qc_config)
    completed_run, source_digest = await current_completed_task_run(
        db, task=task, project=project
    )
    qc_digest = (
        await qc_digest_for_issues(db, run=completed_run, task_id=task.id)
        if completed_run is not None
        else None
    )
    if body and body.expected_qc_digest is not None:
        if qc_digest != body.expected_qc_digest:
            raise HTTPException(
                status_code=409,
                detail={
                    "reason": "mask_qc_digest_conflict",
                    "expected": body.expected_qc_digest,
                    "actual": qc_digest,
                },
            )
    if config.enabled and config.blocking and source_digest is not None:
        if completed_run is None:
            raise HTTPException(
                status_code=409,
                detail={"reason": "mask_qc_current_run_required"},
            )
        blockers = await current_open_task_issues(
            db, task_id=task.id, severity="blocker"
        )
        if blockers:
            raise HTTPException(
                status_code=409,
                detail={
                    "reason": "mask_qc_blockers_present",
                    "issue_ids": [str(issue.id) for issue in blockers],
                    "qc_digest": qc_digest,
                },
            )

    warning_ids = set(body.warning_issue_ids if body else [])
    if warning_ids:
        warnings = await current_open_task_issues(
            db,
            task_id=task.id,
            severity="warning",
            issue_ids=warning_ids,
        )
        if {issue.id for issue in warnings} != warning_ids:
            raise HTTPException(
                status_code=409,
                detail={"reason": "mask_qc_warning_issue_invalid"},
            )

    task.status = "completed"
    now = datetime.now(timezone.utc)
    task.reviewed_at = now
    if task.reviewer_id is None:
        task.reviewer_id = current_user.id
    if task.reviewer_claimed_at is None:
        task.reviewer_claimed_at = now

    review_round_id = _ensure_review_round(task)
    contributor_ids = await _review_round_contributor_snapshot(db, task)
    _record_first_review_fact(
        task,
        reviewed_at=now,
        result="approved",
        contributor_ids=contributor_ids,
    )

    project.completed_tasks = (project.completed_tasks or 0) + 1
    project.review_tasks = max((project.review_tasks or 0) - 1, 0)

    from app.services.batch import BatchService

    batch_svc = BatchService(db)
    await batch_svc.check_auto_transitions(task.batch_id)
    if task.batch_id:
        await batch_svc.recalculate_counters(task.batch_id)

    await AuditService.log(
        db,
        actor=current_user,
        action=AuditAction.TASK_APPROVE,
        target_type="task",
        target_id=str(task_id),
        request=request,
        status_code=200,
        detail={
            "project_id": str(task.project_id),
            "assignee_id": str(task.assignee_id) if task.assignee_id else None,
            "contributor_ids": contributor_ids,
            "review_round_id": str(review_round_id),
            "result": "approved",
            "mask_qc_digest": qc_digest,
            "mask_qc_warning_issue_ids": sorted(str(value) for value in warning_ids),
            "mask_qc_note": (body.note.strip() if body and body.note else None),
        },
    )

    # 通知中心 fan-out：annotator 收到 task.approved（reviewer 自审场景跳过）
    # Publication is deferred until after the commit below so the WS delivery
    # gate can see the committed notification rows.
    pending_notifications: list[Notification] = []
    if task.assignee_id is not None and task.assignee_id != current_user.id:
        from app.services.notification import NotificationService

        notif_svc = NotificationService(db)
        pending_notifications = await notif_svc.notify_many(
            user_ids=[task.assignee_id],
            type="task.approved",
            target_type="task",
            target_id=task.id,
            payload={
                "task_display_id": task.display_id,
                "project_id": str(task.project_id),
                "actor_id": str(current_user.id),
                "actor_name": current_user.name,
            },
        )

    await db.commit()
    if pending_notifications:
        await NotificationService(db).publish_committed(pending_notifications)
    return {"status": "approved", "task_id": str(task_id)}


@router.post("/{task_id}/review/reject")
async def reject_task(
    task_id: uuid.UUID,
    request: Request,
    body: ReviewAction | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    access: ProjectAccess = Depends(require_task_review_write),
):
    task = await _lock_review_task(db, task_id, current_user, access)
    if task.status != "review":
        raise HTTPException(
            status_code=409,
            detail={"reason": "task_not_in_review", "status": task.status},
        )
    await _assert_review_evidence_allowed(db, task=task, user=current_user)
    await _assert_review_owner(db, task=task, user=current_user, access=access)

    reason_type = body.reason_type if body else None
    if reason_type is None:
        raise HTTPException(status_code=422, detail="reject reason_type is required")

    reason = (body.reason if body else None) or None
    reason_text = reason.strip() if reason else None

    task.status = "rejected"
    now = datetime.now(timezone.utc)
    task.reviewed_at = now
    task.reject_reason_type = reason_type
    task.reject_reason = reason_text
    if task.reviewer_id is None:
        task.reviewer_id = current_user.id
    if task.reviewer_claimed_at is None:
        task.reviewer_claimed_at = now
    review_round_id = _ensure_review_round(task)
    contributor_ids = await _review_round_contributor_snapshot(db, task)
    _record_first_review_fact(
        task,
        reviewed_at=now,
        result="rejected",
        contributor_ids=contributor_ids,
    )
    # A2 · the completed round's frozen evidence is preserved in the reject audit
    # before the current authority is invalidated. The annotation contributor
    # accumulator is retained.
    rejected_review_contributor_ids = task.review_contributor_ids
    rejected_review_submitter_id = task.review_submitter_id
    clear_review_contributor_evidence(task)
    await db.flush()

    # ADR-0027 第二段 · 双写到 annotation_feedbacks (kind=reject, anchor=task)
    from app.services.feedback import FeedbackService

    await FeedbackService(db).mirror_task_reject(task, reviewer_id=current_user.id)

    from app.db.models.project import Project

    project = await db.get(Project, task.project_id)
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
        action=AuditAction.TASK_REJECT,
        target_type="task",
        target_id=str(task_id),
        request=request,
        status_code=200,
        detail={
            "project_id": str(task.project_id),
            "assignee_id": str(task.assignee_id) if task.assignee_id else None,
            "reason_type": task.reject_reason_type,
            "reason": task.reject_reason,
            "contributor_ids": contributor_ids,
            "review_contributor_ids": rejected_review_contributor_ids,
            "review_submitter_id": (
                str(rejected_review_submitter_id)
                if rejected_review_submitter_id
                else None
            ),
            "review_round_id": str(review_round_id),
            "result": "rejected",
        },
    )

    pending_notifications = []
    if task.assignee_id is not None and task.assignee_id != current_user.id:
        from app.services.notification import NotificationService

        notif_svc = NotificationService(db)
        pending_notifications = await notif_svc.notify_many(
            user_ids=[task.assignee_id],
            type="task.rejected",
            target_type="task",
            target_id=task.id,
            payload={
                "task_display_id": task.display_id,
                "project_id": str(task.project_id),
                "reject_reason_type": task.reject_reason_type,
                "reject_reason": task.reject_reason,
                "actor_id": str(current_user.id),
                "actor_name": current_user.name,
            },
        )

    await db.commit()
    if pending_notifications:
        await NotificationService(db).publish_committed(pending_notifications)
    return {
        "status": "rejected",
        "task_id": str(task_id),
        "reason_type": task.reject_reason_type,
        "reason": task.reject_reason,
    }
