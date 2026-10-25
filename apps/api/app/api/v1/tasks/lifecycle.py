import uuid
from typing import NoReturn

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.task_lifecycle import TaskLifecycleWorkflow
from app.application.task_submission import TaskSubmissionWorkflow
from app.application.task_workflow_errors import TaskWorkflowError
from app.deps import (
    get_current_user_id,
    get_db,
    get_quality_job_dispatcher,
)
from app.services.audit import audit_origin_from_request
from aap_workspace_contract.access import ProjectAccess
from app.services.quality_job_dispatch import QualityJobDispatcher
from app.api.v1.tasks._shared import require_task_annotation_write_lifecycle

router = APIRouter()


def _raise_workflow_error(error: TaskWorkflowError) -> NoReturn:
    raise HTTPException(status_code=error.status_code, detail=error.detail) from error


@router.post("/{task_id}/submit")
async def submit_task(
    task_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor_id: uuid.UUID = Depends(get_current_user_id),
    access: ProjectAccess = Depends(require_task_annotation_write_lifecycle),
    quality_dispatcher: QualityJobDispatcher = Depends(get_quality_job_dispatcher),
):
    try:
        return await TaskSubmissionWorkflow(db).submit_task(
            task_id,
            actor_id=actor_id,
            access=access,
            origin=audit_origin_from_request(request),
            quality_dispatcher=quality_dispatcher,
        )
    except TaskWorkflowError as exc:
        _raise_workflow_error(exc)


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
    actor_id: uuid.UUID = Depends(get_current_user_id),
    access: ProjectAccess = Depends(require_task_annotation_write_lifecycle),
):
    """标注员跳过任务并附原因，送交审核员复核。

    pending / in_progress 转为 review，不要求已有标注；其他状态返回 409。
    reason 只能为 image_corrupt / no_target / unclear / other；note 可空。
    """
    if body.reason not in _VALID_SKIP_REASONS:
        raise HTTPException(
            status_code=422,
            detail={"reason": "invalid_skip_reason", "value": body.reason},
        )
    try:
        return await TaskLifecycleWorkflow(db).skip_task(
            task_id,
            reason=body.reason,
            note=body.note,
            actor_id=actor_id,
            access=access,
            origin=audit_origin_from_request(request),
        )
    except TaskWorkflowError as exc:
        _raise_workflow_error(exc)


@router.post("/{task_id}/withdraw")
async def withdraw_task(
    task_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor_id: uuid.UUID = Depends(get_current_user_id),
    access: ProjectAccess = Depends(require_task_annotation_write_lifecycle),
):
    """有效标注员撤回尚未认领的送审任务。

    仅允许 review 且 reviewer_claimed_at 为空；审核员认领后不能撤回。
    """
    try:
        return await TaskLifecycleWorkflow(db).withdraw_task(
            task_id,
            actor_id=actor_id,
            access=access,
            origin=audit_origin_from_request(request),
        )
    except TaskWorkflowError as exc:
        _raise_workflow_error(exc)


@router.post("/{task_id}/reopen")
async def reopen_task(
    task_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor_id: uuid.UUID = Depends(get_current_user_id),
    access: ProjectAccess = Depends(require_task_annotation_write_lifecycle),
):
    """有效标注员重新打开已通过任务，管理者按现有授权兜底。

    清空当前审核字段，保留原标注；审计记录原审核人并沿用原通知时序。
    """
    try:
        return await TaskLifecycleWorkflow(db).reopen_task(
            task_id,
            actor_id=actor_id,
            access=access,
            origin=audit_origin_from_request(request),
        )
    except TaskWorkflowError as exc:
        _raise_workflow_error(exc)


@router.post("/{task_id}/accept-rejection")
async def accept_rejection(
    task_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor_id: uuid.UUID = Depends(get_current_user_id),
    access: ProjectAccess = Depends(require_task_annotation_write_lifecycle),
):
    """接受退回，将任务从 rejected 转回 in_progress。

    保留 reject_reason，供重做时查看原审核原因。
    """
    try:
        return await TaskLifecycleWorkflow(db).accept_rejection(
            task_id,
            actor_id=actor_id,
            access=access,
            origin=audit_origin_from_request(request),
        )
    except TaskWorkflowError as exc:
        _raise_workflow_error(exc)
