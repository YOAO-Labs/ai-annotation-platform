import uuid
from typing import Literal, NoReturn

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.task_review import TaskReviewWorkflow
from app.application.task_workflow_errors import TaskWorkflowError
from app.deps import get_current_user_id, get_db
from app.schemas.task import ReviewClaimResponse
from app.services.audit import audit_origin_from_request
from aap_workspace_contract.access import ProjectAccess
from app.api.v1.tasks._shared import require_task_review_write

router = APIRouter()


class ReviewAction(BaseModel):
    """审核员驳回请求；reason_type 必填，reason 保留可空自由文本补充。"""

    reason_type: Literal["missing", "extra", "wrong_label", "wrong_geometry"] | None = (
        None
    )
    reason: str | None = None


class ApproveTaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_qc_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    warning_issue_ids: list[uuid.UUID] = Field(default_factory=list, max_length=500)
    note: str | None = Field(default=None, max_length=2000)


def _raise_workflow_error(error: TaskWorkflowError) -> NoReturn:
    raise HTTPException(status_code=error.status_code, detail=error.detail) from error


@router.post("/{task_id}/review/claim", response_model=ReviewClaimResponse)
async def claim_review(
    task_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor_id: uuid.UUID = Depends(get_current_user_id),
    access: ProjectAccess = Depends(require_task_review_write),
):
    """审核员幂等认领任务。

    首次认领记录审核员和时间，后续调用不覆盖；认领后标注员不能撤回送审。
    """
    try:
        result = await TaskReviewWorkflow(db).claim_review(
            task_id,
            actor_id=actor_id,
            access=access,
            origin=audit_origin_from_request(request),
        )
    except TaskWorkflowError as exc:
        _raise_workflow_error(exc)
    return ReviewClaimResponse(**result)


@router.post("/{task_id}/review/approve")
async def approve_task(
    task_id: uuid.UUID,
    request: Request,
    body: ApproveTaskRequest | None = None,
    db: AsyncSession = Depends(get_db),
    actor_id: uuid.UUID = Depends(get_current_user_id),
    access: ProjectAccess = Depends(require_task_review_write),
):
    try:
        return await TaskReviewWorkflow(db).approve_task(
            task_id,
            actor_id=actor_id,
            access=access,
            origin=audit_origin_from_request(request),
            expected_qc_digest=body.expected_qc_digest if body else None,
            warning_issue_ids=body.warning_issue_ids if body else [],
            note=body.note if body else None,
        )
    except TaskWorkflowError as exc:
        _raise_workflow_error(exc)


@router.post("/{task_id}/review/reject")
async def reject_task(
    task_id: uuid.UUID,
    request: Request,
    body: ReviewAction | None = None,
    db: AsyncSession = Depends(get_db),
    actor_id: uuid.UUID = Depends(get_current_user_id),
    access: ProjectAccess = Depends(require_task_review_write),
):
    try:
        return await TaskReviewWorkflow(db).reject_task(
            task_id,
            actor_id=actor_id,
            access=access,
            origin=audit_origin_from_request(request),
            reason_type=body.reason_type if body else None,
            reason=body.reason if body else None,
        )
    except TaskWorkflowError as exc:
        _raise_workflow_error(exc)
