import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.tasks._shared import (
    _assert_task_editable,
    _assert_task_visible,
    _load_task_or_404,
    require_task_annotation_write,
)
from app.db.models.user import User
from app.deps import get_current_user, get_db, require_scopes
from app.schemas.ai_mask import AiMaskAcceptRequest, AiMaskAcceptResponse
from app.services.project_access import ProjectAccess
from app.services.ai_mask_accept import AiMaskAcceptError, accept_ai_mask_candidate
from app.services.raster_mask_storage import RasterMaskContractError
from app.services.video_collaboration import assert_video_annotation_write_scope
from app.observability.metrics import observe_mask_ai_phase, record_mask_ai_operation

router = APIRouter()


def _parse_if_match(request: Request) -> int | None:
    raw = request.headers.get("If-Match", "").strip()
    if not raw:
        return None
    normalized = raw.removeprefix("W/").strip().removeprefix('"').removesuffix('"')
    try:
        version = int(normalized)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"reason": "invalid_if_match", "message": "Invalid If-Match format"},
        ) from exc
    if version < 1:
        raise HTTPException(
            status_code=400,
            detail={"reason": "invalid_if_match", "message": "Invalid If-Match format"},
        )
    return version


@router.post(
    "/{task_id}/ai-mask-candidates/accept",
    response_model=AiMaskAcceptResponse,
    dependencies=[Depends(require_scopes("annotations:write"))],
)
async def accept_native_ai_mask_candidate(
    task_id: uuid.UUID,
    data: AiMaskAcceptRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    access: ProjectAccess = Depends(require_task_annotation_write),
):
    """Atomically accept one transient native Mask candidate.

    The immutable content object is written before the final database commit. The
    Prediction, lineage, decision, Annotation mutation, and audit event share that
    single commit; a failed transaction leaves only a grace-period GC orphan.
    """

    task = await _load_task_or_404(db, task_id)
    await _assert_task_visible(db, task, current_user, access=access)
    _assert_task_editable(task, current_user, access=access)
    await assert_video_annotation_write_scope(
        db,
        task=task,
        user=current_user,
        segment_id=data.video_segment_id,
        geometry=None,
    )
    expected_version = _parse_if_match(request)
    operation = data.target.mode if data.target.mode == "refine" else "single_frame"
    prompt_family = (
        "bbox"
        if data.prompt_summary.family == "interactive_box"
        else data.prompt_summary.family
    )
    commit_started = time.monotonic()
    try:
        result = await accept_ai_mask_candidate(
            db,
            task_id=task_id,
            data=data,
            current_user=current_user,
            request=request,
            expected_version=expected_version,
            access=access,
        )
    except (AiMaskAcceptError, RasterMaskContractError) as exc:
        outcome = "conflict" if exc.status_code == 409 else "error"
        observe_mask_ai_phase(
            operation=operation,
            phase="commit",
            outcome=outcome,
            duration_seconds=time.monotonic() - commit_started,
        )
        record_mask_ai_operation(
            operation=operation,
            prompt_family=prompt_family,
            output_geometry="mask",
            candidate_count=1,
            decision="accept",
            outcome=outcome,
        )
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    try:
        await db.commit()
    except Exception:
        observe_mask_ai_phase(
            operation=operation,
            phase="commit",
            outcome="error",
            duration_seconds=time.monotonic() - commit_started,
        )
        record_mask_ai_operation(
            operation=operation,
            prompt_family=prompt_family,
            output_geometry="mask",
            candidate_count=1,
            decision="accept",
            outcome="error",
        )
        raise
    observe_mask_ai_phase(
        operation=operation,
        phase="commit",
        outcome="success",
        duration_seconds=time.monotonic() - commit_started,
    )
    record_mask_ai_operation(
        operation=operation,
        prompt_family=prompt_family,
        output_geometry="mask",
        candidate_count=1,
        decision="accept",
        outcome="success",
    )
    response.headers["ETag"] = f'W/"{result.result_version}"'
    return result
