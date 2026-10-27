"""Accept durable project thumbnail backfills at the Image owner boundary."""

from __future__ import annotations

import uuid

from fastapi import HTTPException

from aap_assets_images.contract import (
    AcceptedImageWork,
    ImageBackfillSelection,
    ImageBackfillSpec,
    ImageFailure,
    ImageWorkReference,
    ScheduleImageMediaRequest,
)
from aap_workspace_contract.access import (
    ProjectAccess,
    ProjectWritePolicyFailure,
    ProjectWritePolicyRequest,
)

from app.application.task_workflow_errors import TaskWorkflowError
from app.services.assets_images import image_gateway, image_policy
from app.services.project_access import (
    bind_project_write_policy_resolver,
    restore_project_write_policy_failure,
)

_MANAGER_DENIAL = "仅项目负责人或超级管理员可执行"


async def _manager_access(
    db, actor_id: uuid.UUID, project_id: uuid.UUID
) -> ProjectAccess:
    """Recheck current project authority under the caller's transaction fence."""
    resolver = bind_project_write_policy_resolver(db)
    try:
        access = await resolver.resolve(
            ProjectWritePolicyRequest(actor_id=actor_id, project_id=project_id)
        )
    except ProjectWritePolicyFailure as exc:
        restore_project_write_policy_failure(exc)
    if not access.is_manager:
        raise HTTPException(status_code=403, detail=_MANAGER_DENIAL)
    return access


_STATUS_BY_FAILURE = {
    "invalid_request": 422,
    "permission_denied": 403,
    "account_not_active": 401,
    "invalid_account_role": 403,
    "source_missing": 404,
    "asset_missing": 404,
    "owner_changed": 409,
    "operation_conflict": 409,
    "operation_busy": 409,
    "source_busy": 409,
    "source_changed": 409,
    "resource_budget_exceeded": 413,
    "invalid_response": 502,
    "storage_unavailable": 503,
}


def _receipt_matches(
    accepted: object,
    request: ScheduleImageMediaRequest,
    project_id: uuid.UUID,
) -> bool:
    return (
        isinstance(accepted, AcceptedImageWork)
        and accepted.operation_id == request.operation_id
        and accepted.work.kind == "image_backfill"
        and accepted.work.owner is None
        and isinstance(accepted.work, ImageWorkReference)
        and isinstance(accepted.spec, ImageBackfillSpec)
        and accepted.spec.selection.owner_kind == "task"
        and accepted.spec.selection.project_id == project_id
        and accepted.spec.selection.purpose == "thumbnail"
        and accepted.spec.selection.accepted_before is not None
    )


async def accept_thumbnail_backfill(
    db,
    *,
    actor_id: uuid.UUID,
    project_id: uuid.UUID,
    operation_id: uuid.UUID,
) -> AcceptedImageWork:
    """Ask the Image owner to admit one idempotent project thumbnail backfill."""
    await _manager_access(db, actor_id, project_id)
    request = ScheduleImageMediaRequest(
        operation_id=operation_id,
        owner_kind=None,
        owner_id=None,
        spec=ImageBackfillSpec(
            selection=ImageBackfillSelection(
                owner_kind="task",
                project_id=project_id,
                purpose="thumbnail",
                only_missing=True,
            ),
            policy=image_policy(),
        ),
    )
    try:
        async with image_gateway() as owner:
            accepted = await owner.call("schedule_image_media", request)
    except ImageFailure as exc:
        raise TaskWorkflowError(
            _STATUS_BY_FAILURE.get(exc.code, 503), {"reason": exc.code}
        ) from exc
    if not _receipt_matches(accepted, request, project_id):
        raise TaskWorkflowError(502, {"reason": "invalid_image_backfill_receipt"})
    return accepted
