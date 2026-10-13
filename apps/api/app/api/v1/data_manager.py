from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import (
    assert_project_visible,
    get_current_user,
    get_db,
    require_project_capability,
    require_project_owner,
    require_project_visible,
)
from app.db.models.task import Task
from app.db.models.user import User
from app.services.project_access import (
    ProjectAccess,
    ProjectCapability,
    resolve_project_access,
)
from app.schemas.data_manager import (
    DataManagerMatchesRequest,
    DataManagerMatchesResponse,
    DataManagerEntityLocation,
    DataManagerEntityQueryRequest,
    DataManagerEntityScope,
    DataManagerObjectDetailResponse,
    DataManagerObjectQueryResponse,
    DataManagerSchemaResponse,
    DataManagerSummaryRequest,
    DataManagerSummaryResponse,
    DataManagerTrackDetailResponse,
    DataManagerTrackQueryResponse,
)
from app.schemas.data_manager_actions import (
    DataManagerTaskAssignmentApplyRequest,
    DataManagerTaskAssignmentRequest,
    DataManagerTaskAssignmentResponse,
    DataManagerTaskExportRequest,
)
from app.db.models.async_job import AsyncJobKind
from app.services.data_management.service import (
    DataManagerService,
    build_data_manager_schema,
)
from app.services.data_management.actions import (
    DataManagerTaskActionService,
    assert_idempotent_request_matches,
    find_idempotent_job,
    lock_idempotency_key,
    normalize_idempotency_key,
    request_digest,
)
from app.services.data_management.entities import DataManagerObjectService
from app.services.data_management.entity_filters import validate_entity_view
from app.services.data_management.views import validate_filter
from app.services.data_management.tracks import DataManagerTrackService
from app.services import async_job as async_job_service
from app.services.audit import AuditService
from app.services.exporting.packaging import clean_export_targets
from app.services.data_management.task_filters import visible_tasks_stmt
from app.workers.export import run_export


router = APIRouter()


@router.post(
    "/projects/{project_id}/data-manager/tasks/assignment-preview",
    response_model=DataManagerTaskAssignmentResponse,
)
async def preview_data_manager_task_assignment(
    project_id: uuid.UUID,
    payload: DataManagerTaskAssignmentRequest,
    project=Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(get_current_user),
):
    del project
    return await DataManagerTaskActionService(db).preview_assignment(
        project_id,
        payload,
        actor=actor,
    )


@router.post(
    "/projects/{project_id}/data-manager/tasks/assignment-apply",
    response_model=DataManagerTaskAssignmentResponse,
)
async def apply_data_manager_task_assignment(
    project_id: uuid.UUID,
    payload: DataManagerTaskAssignmentApplyRequest,
    request: Request,
    project=Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(get_current_user),
):
    del project
    result = await DataManagerTaskActionService(db).apply_assignment(
        project_id,
        payload,
        actor=actor,
    )
    await AuditService.log(
        db,
        actor=actor,
        action="data_manager.task_assignment",
        target_type="project",
        target_id=str(project_id),
        request=request,
        status_code=200,
        detail={
            "task_ids": [str(task_id) for task_id in payload.task_ids],
            "succeeded": [str(task_id) for task_id in result.succeeded],
            "failed_count": result.failed_count,
            "skipped_count": result.skipped_count,
        },
    )
    await db.commit()
    return result


@router.post("/projects/{project_id}/data-manager/tasks/export", status_code=202)
async def export_data_manager_tasks(
    project_id: uuid.UUID,
    payload: DataManagerTaskExportRequest,
    request: Request,
    project=Depends(require_project_visible),
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(get_current_user),
    access: ProjectAccess = Depends(
        require_project_capability(ProjectCapability.EXPORT_ANNOTATIONS.value)
    ),
    idempotency_key: str | None = Header(
        default=None,
        alias="Idempotency-Key",
        description="Optional durable identity for retrying this scoped export.",
    ),
):
    task_ids = set(payload.task_ids)
    rows = (
        (
            await db.execute(
                visible_tasks_stmt(
                    project_id,
                    user=actor,
                    project=project,
                    project_role=access.project_role,
                ).where(Task.id.in_(task_ids))
            )
        )
        .scalars()
        .all()
    )
    if set(rows) != task_ids:
        raise HTTPException(
            status_code=404,
            detail="one or more selected tasks are unavailable",
        )

    try:
        targets = clean_export_targets(payload.targets, project.data_type)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    unsupported_partial = {
        "voc",
        "coco-multicamera",
        "nuscenes",
        "kitti",
        "pointmask",
    } & set(targets)
    if unsupported_partial:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "task_scoped_export_unsupported",
                "targets": sorted(unsupported_partial),
                "message": "selected-task export does not support these formats",
            },
        )

    # Data Manager actions are explicitly scoped; preserve the exact selection in
    # both the job payload and worker arguments so a retry cannot widen the scope.
    options = payload.model_dump(
        mode="json",
        exclude={"task_ids", "targets", "scope"},
        exclude_none=True,
    )
    idempotency_key = normalize_idempotency_key(idempotency_key)
    request_key_digest = request_digest(
        {
            "task_ids": [str(task_id) for task_id in payload.task_ids],
            "targets": targets,
            **options,
        }
    )
    job = None
    if idempotency_key is not None:
        await lock_idempotency_key(
            db,
            action="export",
            project_id=project_id,
            actor_id=actor.id,
            key=idempotency_key,
        )
        existing = await find_idempotent_job(
            db,
            kind=AsyncJobKind.EXPORT.value,
            project_id=project_id,
            actor_id=actor.id,
            key=idempotency_key,
        )
        if existing is not None:
            assert_idempotent_request_matches(existing, digest=request_key_digest)
            if existing.status != "pending":
                return {"job_id": str(existing.id), "status": existing.status}
            # A client may retry after the API committed the pending row but
            # before it published to Celery. Reuse that row and republish; the
            # export worker keeps the same job/cache scope.
            job = existing

    job_payload = {
        "targets": targets,
        "format": ",".join(targets),
        "project_id": str(project_id),
        "project_display_id": project.display_id,
        "scope": {"task_ids": [str(task_id) for task_id in payload.task_ids]},
        "task_ids": [str(task_id) for task_id in payload.task_ids],
        **options,
    }
    if idempotency_key is not None:
        job_payload.update(
            {
                "data_manager_idempotency_key": idempotency_key,
                "data_manager_request_digest": request_key_digest,
            }
        )
    if job is None:
        job = await async_job_service.create_job(
            db,
            kind=AsyncJobKind.EXPORT.value,
            user_id=actor.id,
            project_id=project_id,
            payload=job_payload,
        )
    # The worker must see the durable row before Celery can start. This also
    # makes a concurrent retry return the same job instead of dispatching twice
    # when an Idempotency-Key is supplied.
    await db.commit()
    await AuditService.log(
        db,
        actor=actor,
        action="data_manager.task_export",
        target_type="project",
        target_id=str(project_id),
        request=request,
        status_code=202,
        detail={
            "task_ids": [str(task_id) for task_id in payload.task_ids],
            "targets": targets,
        },
    )
    try:
        celery_job = run_export.delay(
            project_id=str(project_id),
            batch_id=None,
            task_ids=[str(task_id) for task_id in payload.task_ids],
            targets=targets,
            opts=options,
            async_job_id=str(job.id),
        )
    except Exception as exc:
        await async_job_service.mark_failed(
            db, job.id, error=f"dispatch failed: {type(exc).__name__}: {exc}"
        )
        await db.commit()
        raise
    job.celery_task_id = celery_job.id
    await db.commit()
    return {"job_id": str(job.id), "status": "queued"}


@router.get(
    "/projects/{project_id}/data-manager/schema",
    response_model=DataManagerSchemaResponse,
)
async def get_data_manager_schema(
    project_id: uuid.UUID,
    entity_scope: DataManagerEntityScope = Query(default="tasks"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    project = await assert_project_visible(project_id, db, user)
    return build_data_manager_schema(project, entity_scope)


@router.post(
    "/projects/{project_id}/data-manager/summary",
    response_model=DataManagerSummaryResponse,
)
async def get_data_manager_summary(
    project_id: uuid.UUID,
    payload: DataManagerSummaryRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    project = await assert_project_visible(project_id, db, user)
    access = await resolve_project_access(db, user=user, project=project)
    validate_filter(payload.filter_json, project=project, user=user)
    return await DataManagerService(db).summary(
        project_id=project_id,
        filter_json=payload.filter_json,
        user=user,
        project=project,
        project_role=access.project_role,
    )


@router.post(
    "/projects/{project_id}/tasks/{task_id}/data-manager/matches",
    response_model=DataManagerMatchesResponse,
)
async def get_data_manager_matches(
    project_id: uuid.UUID,
    task_id: uuid.UUID,
    payload: DataManagerMatchesRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    project = await assert_project_visible(project_id, db, user)
    access = await resolve_project_access(db, user=user, project=project)
    validate_filter(payload.filter_json, project=project, user=user)
    return await DataManagerService(db).matches(
        project_id=project_id,
        task_id=task_id,
        filter_json=payload.filter_json,
        limit=payload.limit,
        offset=payload.offset,
        user=user,
        project=project,
        project_role=access.project_role,
    )


@router.post(
    "/projects/{project_id}/data-manager/objects/query",
    response_model=DataManagerObjectQueryResponse,
)
async def query_data_manager_objects(
    project_id: uuid.UUID,
    payload: DataManagerEntityQueryRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    project = await assert_project_visible(project_id, db, user)
    access = await resolve_project_access(db, user=user, project=project)
    validate_entity_view(
        entity_scope="objects",
        filter_json=payload.filter_json,
        sort_json=payload.sort_json,
        columns_json=payload.columns_json,
        project=project,
    )
    return await DataManagerObjectService(db).query(
        project_id=project_id,
        payload=payload,
        user=user,
        project=project,
        project_role=access.project_role,
    )


@router.get(
    "/projects/{project_id}/data-manager/objects/{annotation_id}/location",
    response_model=DataManagerEntityLocation,
)
async def get_data_manager_object_location(
    project_id: uuid.UUID,
    annotation_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    project = await assert_project_visible(project_id, db, user)
    return await DataManagerObjectService(db).location(
        project_id=project_id,
        annotation_id=annotation_id,
        user=user,
        project=project,
    )


@router.get(
    "/projects/{project_id}/data-manager/objects/{annotation_id}/detail",
    response_model=DataManagerObjectDetailResponse,
)
async def get_data_manager_object_detail(
    project_id: uuid.UUID,
    annotation_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    project = await assert_project_visible(project_id, db, user)
    access = await resolve_project_access(db, user=user, project=project)
    return await DataManagerObjectService(db).detail(
        project_id=project_id,
        annotation_id=annotation_id,
        user=user,
        project=project,
        project_role=access.project_role,
    )


@router.post(
    "/projects/{project_id}/data-manager/tracks/query",
    response_model=DataManagerTrackQueryResponse,
)
async def query_data_manager_tracks(
    project_id: uuid.UUID,
    payload: DataManagerEntityQueryRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    project = await assert_project_visible(project_id, db, user)
    access = await resolve_project_access(db, user=user, project=project)
    validate_entity_view(
        entity_scope="tracks",
        filter_json=payload.filter_json,
        sort_json=payload.sort_json,
        columns_json=payload.columns_json,
        project=project,
    )
    return await DataManagerTrackService(db).query(
        project_id=project_id,
        payload=payload,
        user=user,
        project=project,
        project_role=access.project_role,
    )


@router.get(
    "/projects/{project_id}/data-manager/tracks/{track_ref}/detail",
    response_model=DataManagerTrackDetailResponse,
)
async def get_data_manager_track_detail(
    project_id: uuid.UUID,
    track_ref: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    project = await assert_project_visible(project_id, db, user)
    access = await resolve_project_access(db, user=user, project=project)
    return await DataManagerTrackService(db).detail(
        project_id=project_id,
        track_ref=track_ref,
        user=user,
        project=project,
        project_role=access.project_role,
    )
