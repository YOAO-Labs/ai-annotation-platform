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
from app.services.labeling_core_read_relations import task_row
from aap_workspace_contract.access import (
    ProjectAccess,
    ProjectCapability,
)
from app.services.project_access import resolve_project_access
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
from app.schemas.labeling_assignment import (
    DataManagerTaskAssignmentApplyRequest,
    DataManagerTaskAssignmentRequest,
    DataManagerTaskAssignmentResponse,
)
from app.schemas.data_manager_actions import DataManagerTaskExportRequest
from app.db.models.async_job import AsyncJobKind
from app.services.data_management.service import (
    DataManagerService,
    build_data_manager_schema,
)
from app.services.data_management.actions import (
    assert_idempotent_request_matches,
    find_idempotent_job,
    lock_idempotency_key,
    normalize_idempotency_key,
    request_digest,
)
from app.services.labeling_assignment import LabelingTaskAssignmentService
from app.services.data_management.entities import DataManagerObjectService
from app.services.data_management.entity_filters import validate_entity_view
from app.services.data_management.views import validate_filter
from app.services.data_management.tracks import DataManagerTrackService
from app.services import async_job as async_job_service
from app.services.audit import AuditService
from app.services.exporting.packaging import clean_export_targets
from app.services.data_management.task_filters import visible_tasks_stmt
from app.services.jobs_queue import dispatch_job
from app.services.jobs_dispatch import (
    prepare_job_dispatch,
    publish_prepared_dispatch,
)
from app.services.identity_contracts import IdentityActorFacts


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
    actor: IdentityActorFacts = Depends(get_current_user),
):
    del project
    return await LabelingTaskAssignmentService(db).preview_assignment(
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
    actor: IdentityActorFacts = Depends(get_current_user),
):
    del project
    result = await LabelingTaskAssignmentService(db).apply_assignment(
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
    actor: IdentityActorFacts = Depends(get_current_user),
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
                ).where(task_row.id.in_(task_ids))
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

    task_ids_in_order = [str(task_id) for task_id in payload.task_ids]
    job_payload = {
        "targets": targets,
        "format": ",".join(targets),
        "project_id": str(project_id),
        "project_display_id": project.display_id,
        "scope": {"task_ids": task_ids_in_order},
        "task_ids": task_ids_in_order,
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
        from app.services.exporting.reference_media import capture_export_authority

        from app.services.exporting import output_authority

        job_payload["output_authority"] = await output_authority.capture(
            db, project, actor, request
        )
        job = await async_job_service.create_job(
            db,
            kind=AsyncJobKind.EXPORT.value,
            user_id=actor.id,
            project_id=project_id,
            payload=job_payload,
        )
        from app.services.content_snapshot import ContentError
        from app.services.exporting.media_jobs import capture_job

        try:
            media_bundle = await capture_job(
                db,
                job,
                project,
                batch_id=None,
                targets=targets,
                opts=options,
                task_ids=payload.task_ids,
            )
        except ContentError as exc:
            raise HTTPException(exc.status, detail={"code": exc.code}) from None
        authority = await capture_export_authority(
            db,
            project,
            actor,
            request,
            task_ids=payload.task_ids,
            frozen_snapshot=media_bundle.input_snapshot if media_bundle else None,
        )
        if authority:
            job.payload = {**job.payload, "export_authority": authority}
    execution_id = str(uuid.uuid4())
    dispatch_args = []
    dispatch_kwargs = {
        "project_id": str(project_id),
        "batch_id": None,
        "task_ids": task_ids_in_order,
        "targets": targets,
        "opts": options,
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
            "task_ids": task_ids_in_order,
            "targets": targets,
        },
    )
    try:
        published_execution_id = await publish_prepared_dispatch(
            db,
            lease,
            publish=lambda: dispatch_job(
                task_name="app.workers.export.run_export",
                execution_id=execution_id,
                args=dispatch_args,
                kwargs=dispatch_kwargs,
            ),
        )
    except Exception:
        from app.services.exporting.media_jobs import fail_dispatch

        await fail_dispatch(db, job.id)
        await db.commit()
        raise
    async_job_service.bind_execution_id(job, published_execution_id)
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
    user: IdentityActorFacts = Depends(get_current_user),
):
    project = await assert_project_visible(project_id, db, user.id)
    return build_data_manager_schema(project, entity_scope)


@router.post(
    "/projects/{project_id}/data-manager/summary",
    response_model=DataManagerSummaryResponse,
)
async def get_data_manager_summary(
    project_id: uuid.UUID,
    payload: DataManagerSummaryRequest,
    db: AsyncSession = Depends(get_db),
    user: IdentityActorFacts = Depends(get_current_user),
):
    project = await assert_project_visible(project_id, db, user.id)
    access = await resolve_project_access(db, actor_id=user.id, project=project)
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
    user: IdentityActorFacts = Depends(get_current_user),
):
    project = await assert_project_visible(project_id, db, user.id)
    access = await resolve_project_access(db, actor_id=user.id, project=project)
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
    user: IdentityActorFacts = Depends(get_current_user),
):
    project = await assert_project_visible(project_id, db, user.id)
    access = await resolve_project_access(db, actor_id=user.id, project=project)
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
    user: IdentityActorFacts = Depends(get_current_user),
):
    project = await assert_project_visible(project_id, db, user.id)
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
    user: IdentityActorFacts = Depends(get_current_user),
):
    project = await assert_project_visible(project_id, db, user.id)
    access = await resolve_project_access(db, actor_id=user.id, project=project)
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
    user: IdentityActorFacts = Depends(get_current_user),
):
    project = await assert_project_visible(project_id, db, user.id)
    access = await resolve_project_access(db, actor_id=user.id, project=project)
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
    user: IdentityActorFacts = Depends(get_current_user),
):
    project = await assert_project_visible(project_id, db, user.id)
    access = await resolve_project_access(db, actor_id=user.id, project=project)
    return await DataManagerTrackService(db).detail(
        project_id=project_id,
        track_ref=track_ref,
        user=user,
        project=project,
        project_role=access.project_role,
    )
