from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import task_access
from app.api.v1.tasks._shared import (
    _load_task_or_404,
)
from app.deps import get_current_user, get_db, require_scopes
from app.schemas.task import TaskMaskCapabilitiesResponse
from app.services.raster_mask_capabilities import evaluate_raster_mask_capabilities
from app.services.project_access import get_workspace_project
from app.services.raster_mask_storage import MAX_RLE_OBJECT_BYTES
from app.utils.raster_mask_rle import (
    MAX_IMAGE_MASK_DIMENSION,
    MAX_IMAGE_MASK_PIXELS,
    MAX_MASK_RUNS,
)
from app.services.identity_contracts import IdentityActorFacts

router = APIRouter()


@router.get(
    "/{task_id}/mask-capabilities",
    response_model=TaskMaskCapabilitiesResponse,
    dependencies=[Depends(require_scopes("annotations:read"))],
)
async def get_mask_capabilities(
    task_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(get_current_user),
) -> TaskMaskCapabilitiesResponse:
    task = await _load_task_or_404(db, task_id)
    access = await task_access.resolve_task_access(db, task, current_user.id)
    await task_access.assert_task_visible(db, task, current_user.id, access=access)

    project = await get_workspace_project(db, actor_id=None, project_id=task.project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Task not found")

    capabilities = evaluate_raster_mask_capabilities(project)
    write_enabled = capabilities.write_enabled
    legacy_polygon_commit_enabled = capabilities.legacy_polygon_commit_enabled
    reason = capabilities.reason
    try:
        task_access.assert_task_editable(task, current_user.id, access=access)
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        if exc.status_code != 409 or detail.get("reason") != "task_locked":
            raise
        write_enabled = False
        legacy_polygon_commit_enabled = False
        reason = "task_locked"

    return TaskMaskCapabilitiesResponse(
        read_enabled=capabilities.read_enabled,
        write_enabled=write_enabled,
        legacy_polygon_commit_enabled=legacy_polygon_commit_enabled,
        project_enabled=capabilities.project_enabled,
        region_enabled=capabilities.region_enabled,
        reason=reason,
        max_dimension=MAX_IMAGE_MASK_DIMENSION,
        max_pixels=MAX_IMAGE_MASK_PIXELS,
        max_runs=MAX_MASK_RUNS,
        max_bytes=MAX_RLE_OBJECT_BYTES,
    )
