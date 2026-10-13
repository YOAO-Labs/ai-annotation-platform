from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.tasks import (
    _attach_dimensions_batch,
    _attach_image_pyramids_batch,
    _task_with_url,
)
from app.deps import get_current_user, get_db
from app.db.models.project_task_view import ProjectTaskView
from app.db.models.user import User
from app.services.project_access import ProjectAccess, resolve_project_access_by_id
from app.schemas.task_view import (
    DataManagerTaskOut,
    ProjectTaskQueryRequest,
    ProjectTaskQueryResponse,
    ProjectTaskViewCopyRequest,
    ProjectTaskViewCreate,
    ProjectTaskViewListResponse,
    ProjectTaskViewOut,
    ProjectTaskViewUpdate,
)
from app.schemas.data_manager import DataManagerEntityScope
from app.services.data_management.entity_filters import (
    builtin_entity_views,
    count_entity_filters,
    invalid_entity_filter_fields,
)
from app.services.audit import AuditAction, AuditService
from app.services.data_management.schema import build_data_manager_schema
from app.services.data_management.views import (
    TaskViewService,
    builtin_views,
    invalid_filter_fields,
    validate_columns,
    validate_filter,
    validate_sort,
)
from app.services.user_brief import resolve_briefs

router = APIRouter()


def _can_manage_project(access: ProjectAccess) -> bool:
    return access.is_manager


def _assert_can_write_view(access: ProjectAccess, view: ProjectTaskView) -> None:
    if view.visibility == "private":
        if view.owner_id == access.user_id or access.is_super_admin:
            return
        raise HTTPException(status_code=403, detail="Only the view owner can edit it")
    if _can_manage_project(access):
        return
    raise HTTPException(
        status_code=403, detail="Only project admins can edit shared views"
    )


def _sort_to_json(payload) -> list[dict]:
    return [
        item.model_dump() if hasattr(item, "model_dump") else dict(item)
        for item in payload
    ]


async def _commit_or_duplicate(db: AsyncSession) -> None:
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=409, detail="Task view name already exists"
        ) from exc


def _view_out(
    view: ProjectTaskView,
    task_count: int | None = None,
    invalid_fields: list[str] | None = None,
) -> ProjectTaskViewOut:
    out = ProjectTaskViewOut.model_validate(view, from_attributes=True)
    out.result_count = task_count
    out.task_count = task_count if view.entity_scope == "tasks" else None
    out.invalid_fields = invalid_fields or []
    return out


def _row_value(row, key: str, default=None):
    return row._mapping.get(key, default)


@router.get(
    "/projects/{project_id}/task-views", response_model=ProjectTaskViewListResponse
)
async def list_task_views(
    project_id: uuid.UUID,
    entity_scope: DataManagerEntityScope = Query(default="tasks"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    project, access = await resolve_project_access_by_id(
        db, user=user, project_id=project_id
    )
    build_data_manager_schema(project, entity_scope)
    svc = TaskViewService(db)
    builtins = (
        builtin_views(project_id, project=project)
        if entity_scope == "tasks"
        else builtin_entity_views(project_id, entity_scope)
    )
    saved = await svc.list_views(project_id, user.id, entity_scope)
    # 单条聚合查询算出全部视图计数，避免每个视图一次往返 (N+1)。
    entries = [*builtins, *saved]
    invalid_by_index = [
        (
            invalid_filter_fields(
                entry["filter_json"] if isinstance(entry, dict) else entry.filter_json,
                project,
            )
            if entity_scope == "tasks"
            else invalid_entity_filter_fields(
                entry["filter_json"] if isinstance(entry, dict) else entry.filter_json,
                entity_scope,
                project,
            )
        )
        for entry in entries
    ]
    valid_filters = [
        entry["filter_json"] if isinstance(entry, dict) else entry.filter_json
        for entry, invalid in zip(entries, invalid_by_index)
        if not invalid
    ]
    valid_counts = (
        await svc.count_for_filters(
            project_id,
            valid_filters,
            user=user,
            project=project,
            project_role=access.project_role,
        )
        if entity_scope == "tasks"
        else await count_entity_filters(
            db,
            project_id=project_id,
            entity_scope=entity_scope,
            filters=valid_filters,
            user=user,
            project=project,
            project_role=access.project_role,
        )
    )
    counts: list[int | None] = []
    count_index = 0
    for invalid in invalid_by_index:
        if invalid:
            counts.append(None)
        else:
            counts.append(valid_counts[count_index])
            count_index += 1
    items: list[ProjectTaskViewOut] = []
    for i, (builtin, count) in enumerate(zip(builtins, counts)):
        items.append(
            ProjectTaskViewOut(
                **builtin,
                task_count=count if entity_scope == "tasks" else None,
                result_count=count,
                invalid_fields=invalid_by_index[i],
            )
        )
    for index, (view, count) in enumerate(zip(saved, counts[len(builtins) :])):
        items.append(
            _view_out(
                view,
                task_count=count,
                invalid_fields=invalid_by_index[len(builtins) + index],
            )
        )
    return ProjectTaskViewListResponse(items=items)


@router.post(
    "/projects/{project_id}/task-views",
    response_model=ProjectTaskViewOut,
    status_code=201,
)
async def create_task_view(
    project_id: uuid.UUID,
    payload: ProjectTaskViewCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    project, access = await resolve_project_access_by_id(
        db, user=user, project_id=project_id
    )
    if payload.visibility == "project" and not _can_manage_project(access):
        raise HTTPException(
            status_code=403, detail="Only project admins can create shared views"
        )
    svc = TaskViewService(db)
    view = await svc.create_view(
        project_id=project_id,
        owner_id=user.id,
        name=payload.name,
        visibility=payload.visibility,
        entity_scope=payload.entity_scope,
        filter_json=payload.filter_json,
        sort_json=_sort_to_json(payload.sort_json),
        columns_json=payload.columns_json,
        project=project,
    )
    await AuditService.log(
        db,
        actor=user,
        action=AuditAction.PROJECT_UPDATE,
        target_type="project_task_view",
        target_id=view.id,
        request=request,
        status_code=201,
        detail={"project_id": str(project_id), "visibility": view.visibility},
    )
    await _commit_or_duplicate(db)
    return _view_out(view)


@router.get(
    "/projects/{project_id}/task-views/{view_id}", response_model=ProjectTaskViewOut
)
async def get_task_view(
    project_id: uuid.UUID,
    view_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    project, access = await resolve_project_access_by_id(
        db, user=user, project_id=project_id
    )
    svc = TaskViewService(db)
    view = await svc.get_view(project_id, view_id, user.id)
    invalid = (
        invalid_filter_fields(view.filter_json, project)
        if view.entity_scope == "tasks"
        else invalid_entity_filter_fields(view.filter_json, view.entity_scope, project)
    )
    count = None
    if not invalid:
        if view.entity_scope == "tasks":
            count = await svc.count_for_filter(
                project_id,
                view.filter_json,
                user=user,
                project=project,
                project_role=access.project_role,
            )
        else:
            count = (
                await count_entity_filters(
                    db,
                    project_id=project_id,
                    entity_scope=view.entity_scope,
                    filters=[view.filter_json],
                    user=user,
                    project=project,
                    project_role=access.project_role,
                )
            )[0]
    return _view_out(view, task_count=count, invalid_fields=invalid)


@router.patch(
    "/projects/{project_id}/task-views/{view_id}", response_model=ProjectTaskViewOut
)
async def update_task_view(
    project_id: uuid.UUID,
    view_id: uuid.UUID,
    payload: ProjectTaskViewUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    project, access = await resolve_project_access_by_id(
        db, user=user, project_id=project_id
    )
    svc = TaskViewService(db)
    view = await svc.get_view(project_id, view_id, user.id)
    _assert_can_write_view(access, view)
    next_visibility = payload.visibility
    if next_visibility == "project" and not _can_manage_project(access):
        raise HTTPException(
            status_code=403, detail="Only project admins can share views"
        )
    view = await svc.update_view(
        view,
        name=payload.name,
        visibility=payload.visibility,
        filter_json=payload.filter_json,
        sort_json=_sort_to_json(payload.sort_json)
        if payload.sort_json is not None
        else None,
        columns_json=payload.columns_json,
        project=project,
    )
    await AuditService.log(
        db,
        actor=user,
        action=AuditAction.PROJECT_UPDATE,
        target_type="project_task_view",
        target_id=view.id,
        request=request,
        status_code=200,
        detail={"project_id": str(project_id), "visibility": view.visibility},
    )
    await _commit_or_duplicate(db)
    return _view_out(view)


@router.delete("/projects/{project_id}/task-views/{view_id}", status_code=204)
async def delete_task_view(
    project_id: uuid.UUID,
    view_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    project, access = await resolve_project_access_by_id(
        db, user=user, project_id=project_id
    )
    svc = TaskViewService(db)
    view = await svc.get_view(project_id, view_id, user.id)
    _assert_can_write_view(access, view)
    await AuditService.log(
        db,
        actor=user,
        action=AuditAction.PROJECT_UPDATE,
        target_type="project_task_view",
        target_id=view.id,
        request=request,
        status_code=204,
        detail={"project_id": str(project_id), "deleted": True},
    )
    await svc.delete_view(view)
    await db.commit()
    return None


@router.post(
    "/projects/{project_id}/task-views/{view_id}/copy",
    response_model=ProjectTaskViewOut,
    status_code=201,
)
async def copy_task_view(
    project_id: uuid.UUID,
    view_id: uuid.UUID,
    payload: ProjectTaskViewCopyRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    project, access = await resolve_project_access_by_id(
        db, user=user, project_id=project_id
    )
    svc = TaskViewService(db)
    source = await svc.get_view(project_id, view_id, user.id)
    visibility = payload.visibility or source.visibility
    if visibility == "project" and not _can_manage_project(access):
        raise HTTPException(
            status_code=403, detail="Only project admins can create shared views"
        )
    view = await svc.create_view(
        project_id=project_id,
        owner_id=user.id,
        name=payload.name or f"{source.name} 副本",
        visibility=visibility,
        entity_scope=source.entity_scope,
        filter_json=source.filter_json,
        sort_json=source.sort_json,
        columns_json=source.columns_json,
        project=project,
    )
    await AuditService.log(
        db,
        actor=user,
        action=AuditAction.PROJECT_UPDATE,
        target_type="project_task_view",
        target_id=view.id,
        request=request,
        status_code=201,
        detail={"project_id": str(project_id), "copied_from": str(source.id)},
    )
    await _commit_or_duplicate(db)
    return _view_out(view)


@router.post(
    "/projects/{project_id}/tasks/query", response_model=ProjectTaskQueryResponse
)
async def query_project_tasks(
    project_id: uuid.UUID,
    payload: ProjectTaskQueryRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    project, access = await resolve_project_access_by_id(
        db, user=user, project_id=project_id
    )
    validate_filter(payload.filter_json, project=project, user=user)
    validate_sort(_sort_to_json(payload.sort_json))
    validate_columns(payload.columns_json)
    svc = TaskViewService(db)
    rows, total = await svc.query_tasks(
        project_id=project_id,
        filter_json=payload.filter_json,
        sort_json=_sort_to_json(payload.sort_json),
        columns_json=payload.columns_json,
        limit=payload.limit,
        offset=payload.offset,
        user=user,
        project=project,
        project_role=access.project_role,
    )
    tasks = [row[0] for row in rows]
    dims = await _attach_dimensions_batch(db, tasks)
    pyramids = await _attach_image_pyramids_batch(db, tasks, dims)
    user_ids = {t.assignee_id for t in tasks if t.assignee_id} | {
        t.reviewer_id for t in tasks if t.reviewer_id
    }
    user_ids.update(
        user_id
        for row in rows
        for key in ("effective_assignee_id", "effective_reviewer_id")
        if (user_id := _row_value(row, key)) is not None
    )
    briefs = await resolve_briefs(db, user_ids) if user_ids else {}
    items: list[DataManagerTaskOut] = []
    for row in rows:
        task = row[0]
        base = _task_with_url(
            task,
            *dims.get(task.id, (None, None, None, None, None)),
            image_pyramid=pyramids.get(task.id),
            briefs=briefs,
        ).model_dump()
        base.update(
            {
                "effective_assignee": briefs.get(
                    str(_row_value(row, "effective_assignee_id"))
                ),
                "effective_reviewer": briefs.get(
                    str(_row_value(row, "effective_reviewer_id"))
                ),
                "annotation_count": task.total_annotations,
                "prediction_count": task.total_predictions,
                "avg_prediction_confidence": _row_value(
                    row, "avg_prediction_confidence"
                ),
                "unresolved_issue_count": int(
                    _row_value(row, "unresolved_issue_count", 0) or 0
                ),
                # Legacy alias of the corrected unresolved-issue count.
                "unresolved_feedback_count": int(
                    _row_value(row, "unresolved_issue_count", 0) or 0
                ),
                "comment_count": int(_row_value(row, "comment_count", 0) or 0),
                "model_versions": list(_row_value(row, "model_versions", []) or []),
                "scene_name": _row_value(row, "scene_name"),
                "frame_index": _row_value(row, "frame_index"),
                "last_activity_at": _row_value(row, "last_activity_at"),
                "annotation_source_counts": {
                    source: int(_row_value(row, f"source_{source}_count", 0) or 0)
                    for source in (
                        "manual",
                        "prediction_based",
                        "ai_tracker",
                        "interpolated",
                    )
                },
                "track_count": int(_row_value(row, "track_count", 0) or 0),
                "pending_prediction_shape_count": int(
                    _row_value(row, "pending_prediction_shape_count", 0) or 0
                ),
                "low_confidence_prediction_shape_count": int(
                    _row_value(row, "low_confidence_prediction_shape_count", 0) or 0
                ),
                "pending_tracker_job_count": int(
                    _row_value(row, "pending_tracker_job_count", 0) or 0
                ),
                "keyframe_count": int(_row_value(row, "keyframe_count", 0) or 0),
                "outside_range_count": int(
                    _row_value(row, "outside_range_count", 0) or 0
                ),
                "camera_count": int(_row_value(row, "camera_count", 0) or 0),
                "calibration_issue_count": int(
                    _row_value(row, "calibration_issue_count", 0) or 0
                ),
                "scene_total_frames": _row_value(row, "scene_total_frames"),
            }
        )
        items.append(DataManagerTaskOut(**base))
    return ProjectTaskQueryResponse(
        items=items,
        total=total,
        limit=payload.limit,
        offset=payload.offset,
    )


@router.get(
    "/projects/{project_id}/task-views/{view_id}/tasks",
    response_model=ProjectTaskQueryResponse,
)
async def query_task_view_tasks(
    project_id: uuid.UUID,
    view_id: uuid.UUID,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _project, _access = await resolve_project_access_by_id(
        db, user=user, project_id=project_id
    )
    svc = TaskViewService(db)
    view = await svc.get_view(project_id, view_id, user.id)
    if view.entity_scope != "tasks":
        raise HTTPException(status_code=422, detail="Saved view is not a task view")
    return await query_project_tasks(
        project_id,
        ProjectTaskQueryRequest(
            filter_json=view.filter_json,
            sort_json=view.sort_json,
            columns_json=view.columns_json,
            limit=limit,
            offset=offset,
        ),
        db,
        user,
    )
