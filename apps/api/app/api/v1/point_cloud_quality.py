from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.tasks._shared import _assert_task_visible, _visible_task_ids
from app.db.models.annotation import Annotation
from app.db.models.point_cloud_quality import (
    PointCloudQualityEvaluation,
    PointCloudQualityIssue,
    PointCloudQualityRun,
)
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.user import User
from app.deps import (
    get_current_user,
    get_db,
    require_project_capability,
    require_project_visible,
    require_active_task_actor,
)
from app.services.project_access import (
    ProjectAccess,
    ProjectCapability,
    assert_capability,
    resolve_project_access,
)
from app.schemas.point_cloud_quality import (
    POINT_CLOUD_QUALITY_RULE_CODES,
    PointCloudQualityIssueOut,
    PointCloudQualityIssuePage,
    PointCloudQualityIssuePatch,
    PointCloudQualityEvaluationCreate,
    PointCloudQualityEvaluationOut,
    PointCloudQualityEvaluationPage,
    PointCloudQualityRunOut,
    PointCloudQualityRunRequest,
)
from app.services import async_job as async_job_svc
from app.services.audit import AuditAction, AuditService
from app.services.point_cloud_quality.service import (
    PointCloudQualityError,
    create_quality_run,
    dispatch_quality_run,
    list_issues,
    refresh_issue_staleness,
)
from app.services.point_cloud_quality.evaluation import (
    create_evaluation,
    list_evaluations,
    promote_evaluation,
)


router = APIRouter(dependencies=[Depends(require_active_task_actor)])


def _raise_quality_error(exc: PointCloudQualityError) -> None:
    raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


def _assert_review_authority(access: ProjectAccess) -> None:
    """QC review reads/writes require a manager or a project reviewer."""

    if access.is_manager or ProjectCapability.REVIEW_WRITE.value in access.capabilities:
        return
    raise HTTPException(status_code=403, detail="缺少项目权限: review.write")


def _run_out(run: PointCloudQualityRun, *, reused: bool = False):
    return PointCloudQualityRunOut.model_validate(run).model_copy(
        update={"reused": reused}
    )


async def _assert_request_visible(
    db: AsyncSession,
    *,
    project: Project,
    user: User,
    access: ProjectAccess,
    body: PointCloudQualityRunRequest,
) -> None:
    if body.scope in {"project", "scene_ids"}:
        if not access.is_manager:
            raise HTTPException(
                status_code=403,
                detail="project and scene quality scans require project owner",
            )
        return
    task_ids: set[uuid.UUID]
    if body.scope == "task_ids":
        task_ids = set(body.task_ids)
    else:
        rows = (
            await db.execute(
                select(Annotation.id, Annotation.task_id).where(
                    Annotation.project_id == project.id,
                    Annotation.id.in_(body.annotation_ids),
                )
            )
        ).all()
        if {row.id for row in rows} != set(body.annotation_ids):
            raise HTTPException(status_code=404, detail="3D annotation not found")
        task_ids = {row.task_id for row in rows}
    tasks = list(
        (
            await db.execute(
                select(Task).where(Task.project_id == project.id, Task.id.in_(task_ids))
            )
        ).scalars()
    )
    if {row.id for row in tasks} != task_ids:
        raise HTTPException(status_code=404, detail="Task not found")
    for task in tasks:
        await _assert_task_visible(db, task, user, access=access)


async def _assert_issue_visible(
    db: AsyncSession, issue: PointCloudQualityIssue, user: User, *, lock: bool = False
) -> tuple[Project, ProjectAccess]:
    project = await db.get(Project, issue.project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    access = await resolve_project_access(
        db, user=user, project=project, lock_membership=lock
    )
    if access.is_manager:
        return project, access
    if issue.task_id is None:
        raise HTTPException(status_code=403, detail="Quality issue not visible")
    task = await db.get(Task, issue.task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    await _assert_task_visible(db, task, user, access=access)
    return project, access


async def _lock_task_nowait(db: AsyncSession, task_id: uuid.UUID) -> Task:
    try:
        task = (
            await db.execute(
                select(Task)
                .where(Task.id == task_id)
                .with_for_update(nowait=True)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
    except DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", None) in {"55P03", "40P01", "40001"}:
            raise HTTPException(
                status_code=409, detail={"reason": "task_busy"}
            ) from exc
        raise
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@router.post(
    "/projects/{project_id}/point-cloud-quality/runs",
    response_model=PointCloudQualityRunOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_run(
    body: PointCloudQualityRunRequest,
    project: Project = Depends(require_project_visible),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    access: ProjectAccess = Depends(
        require_project_capability(ProjectCapability.REVIEW_WRITE.value)
    ),
):
    # Re-resolve current authority under a membership lock for the write path and
    # re-assert the capability on the fresh access.
    access = await resolve_project_access(
        db, user=current_user, project=project, lock_membership=True
    )
    assert_capability(access, ProjectCapability.REVIEW_WRITE.value)
    await _assert_request_visible(
        db, project=project, user=current_user, access=access, body=body
    )
    try:
        run, job, created = await create_quality_run(
            db, project=project, actor_id=current_user.id, request=body
        )
    except PointCloudQualityError as exc:
        _raise_quality_error(exc)
    await db.commit()
    if created and job is not None:
        try:
            await dispatch_quality_run(db, run_id=run.id, async_job_id=job.id)
        except PointCloudQualityError as exc:
            run = await db.get(PointCloudQualityRun, run.id)
            if run is not None:
                run.status = "failed"
                run.error_message = str(exc.detail)[:4000]
                run.completed_at = datetime.now(timezone.utc)
            await async_job_svc.mark_failed(db, job.id, error=str(exc.detail))
            await db.commit()
            _raise_quality_error(exc)
    await db.refresh(run)
    return _run_out(run, reused=not created)


@router.get(
    "/projects/{project_id}/point-cloud-quality/runs/{run_id}",
    response_model=PointCloudQualityRunOut,
)
async def get_run(
    run_id: uuid.UUID,
    project: Project = Depends(require_project_visible),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    access: ProjectAccess = Depends(
        require_project_capability(ProjectCapability.REVIEW_WRITE.value)
    ),
):
    run = await db.get(PointCloudQualityRun, run_id)
    if run is None or run.project_id != project.id:
        raise HTTPException(status_code=404, detail="Point cloud quality run not found")
    if not access.is_manager:
        task_ids = [uuid.UUID(value) for value in run.scope_json.get("task_ids", [])]
        if await _visible_task_ids(
            db, project, current_user, task_ids, access=access
        ) != set(task_ids):
            raise HTTPException(status_code=403, detail="Quality run not visible")
    return _run_out(run)


@router.get(
    "/projects/{project_id}/point-cloud-quality/issues",
    response_model=PointCloudQualityIssuePage,
)
async def get_issues(
    project: Project = Depends(require_project_visible),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    access: ProjectAccess = Depends(
        require_project_capability(ProjectCapability.REVIEW_WRITE.value)
    ),
    issue_status: Literal["open", "resolved", "wont_fix", "stale"] | None = Query(
        default=None, alias="status"
    ),
    severity: Literal["blocker", "warning", "info"] | None = None,
    code: str | None = None,
    scene_id: uuid.UUID | None = None,
    task_id: uuid.UUID | None = None,
    annotation_id: uuid.UUID | None = None,
    scene_track_id: uuid.UUID | None = None,
    frame: int | None = Query(default=None, ge=0),
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    if code is not None and code not in POINT_CLOUD_QUALITY_RULE_CODES:
        raise HTTPException(status_code=422, detail="Unknown point cloud quality code")
    allowed_task_ids: set[uuid.UUID] | None = None
    if not access.is_manager:
        all_task_ids = list(
            (
                await db.execute(select(Task.id).where(Task.project_id == project.id))
            ).scalars()
        )
        allowed_task_ids = await _visible_task_ids(
            db, project, current_user, all_task_ids, access=access
        )
    rows, total = await list_issues(
        db,
        project_id=project.id,
        status=issue_status,
        severity=severity,
        code=code,
        scene_id=scene_id,
        task_id=task_id,
        annotation_id=annotation_id,
        scene_track_id=scene_track_id,
        frame=frame,
        allowed_task_ids=allowed_task_ids,
        limit=limit,
        offset=offset,
    )
    await db.flush()
    for row in rows:
        await db.refresh(row)
    payload = [PointCloudQualityIssueOut.model_validate(row) for row in rows]
    await db.commit()
    return PointCloudQualityIssuePage(
        items=payload,
        total=total,
    )


@router.get(
    "/point-cloud-quality/issues/{issue_id}",
    response_model=PointCloudQualityIssueOut,
)
async def get_issue(
    issue_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    issue = await db.get(PointCloudQualityIssue, issue_id)
    if issue is None:
        raise HTTPException(status_code=404, detail="Quality issue not found")
    _project, access = await _assert_issue_visible(db, issue, current_user)
    _assert_review_authority(access)
    await refresh_issue_staleness(db, issue)
    await db.flush()
    await db.refresh(issue)
    payload = PointCloudQualityIssueOut.model_validate(issue)
    await db.commit()
    return payload


@router.patch(
    "/point-cloud-quality/issues/{issue_id}",
    response_model=PointCloudQualityIssueOut,
)
async def patch_issue(
    issue_id: uuid.UUID,
    body: PointCloudQualityIssuePatch,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    issue = await db.get(PointCloudQualityIssue, issue_id)
    if issue is None:
        raise HTTPException(status_code=404, detail="Quality issue not found")
    # Account-first: resolve the locked membership, then take bounded task and
    # resource locks so a busy row cannot block indefinitely.
    _project, access = await _assert_issue_visible(db, issue, current_user, lock=True)
    _assert_review_authority(access)
    if issue.task_id is not None:
        task = await db.get(Task, issue.task_id, populate_existing=True)
        if task is not None:
            await _lock_task_nowait(db, task.id)
    try:
        issue = (
            await db.execute(
                select(PointCloudQualityIssue)
                .where(PointCloudQualityIssue.id == issue_id)
                .with_for_update(nowait=True)
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
    except DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", None) in {"55P03", "40P01", "40001"}:
            raise HTTPException(
                status_code=409, detail={"reason": "point_cloud_quality_issue_busy"}
            ) from exc
        raise
    if await refresh_issue_staleness(db, issue):
        raise HTTPException(
            status_code=409, detail={"reason": "point_cloud_quality_issue_stale"}
        )
    previous = issue.status
    previous_verdict = issue.review_verdict
    issue.status = body.status
    issue.resolution_reason = (body.reason or "").strip() or None
    if body.status == "open":
        issue.resolved_by_id = None
        issue.resolved_at = None
        issue.review_verdict = None
        issue.review_note = None
        issue.reviewed_by_id = None
        issue.reviewed_at = None
    else:
        issue.resolved_by_id = current_user.id
        issue.resolved_at = datetime.now(timezone.utc)
        issue.review_verdict = body.review_verdict
        issue.review_note = (body.review_note or "").strip() or None
        issue.reviewed_by_id = current_user.id if body.review_verdict else None
        issue.reviewed_at = datetime.now(timezone.utc) if body.review_verdict else None
    await AuditService.log(
        db,
        actor=current_user,
        action=AuditAction.POINT_CLOUD_QUALITY_ISSUE_STATUS,
        target_type="point_cloud_quality_issue",
        target_id=issue.id,
        request=request,
        status_code=200,
        detail={
            "from": previous,
            "to": issue.status,
            "reason": issue.resolution_reason,
            "verdict_from": previous_verdict,
            "verdict_to": issue.review_verdict,
            "review_note": issue.review_note,
        },
    )
    await db.commit()
    await db.refresh(issue)
    return PointCloudQualityIssueOut.model_validate(issue)


@router.post(
    "/projects/{project_id}/point-cloud-quality/evaluations",
    response_model=PointCloudQualityEvaluationOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_project_evaluation(
    body: PointCloudQualityEvaluationCreate,
    request: Request,
    project: Project = Depends(require_project_visible),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _access: ProjectAccess = Depends(
        require_project_capability(ProjectCapability.PROJECT_MANAGE.value)
    ),
):
    fresh_access = await resolve_project_access(
        db, user=current_user, project=project, lock_membership=True
    )
    assert_capability(fresh_access, ProjectCapability.PROJECT_MANAGE.value)
    try:
        evaluation = await create_evaluation(
            db,
            project=project,
            actor_id=current_user.id,
            candidate=body.candidate_config,
        )
    except PointCloudQualityError as exc:
        _raise_quality_error(exc)
    await AuditService.log(
        db,
        actor=current_user,
        action=AuditAction.POINT_CLOUD_QUALITY_EVALUATION_CREATE,
        target_type="point_cloud_quality_evaluation",
        target_id=evaluation.id,
        request=request,
        status_code=201,
        detail={
            "project_id": str(project.id),
            "baseline_config_revision": evaluation.baseline_config_revision,
            "sample_count": evaluation.sample_count,
            "gate_status": evaluation.gate_status,
        },
    )
    await db.commit()
    await db.refresh(evaluation)
    return PointCloudQualityEvaluationOut.model_validate(evaluation)


@router.get(
    "/projects/{project_id}/point-cloud-quality/evaluations",
    response_model=PointCloudQualityEvaluationPage,
)
async def get_project_evaluations(
    project: Project = Depends(require_project_visible),
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
    _access: ProjectAccess = Depends(
        require_project_capability(ProjectCapability.REVIEW_WRITE.value)
    ),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    rows, total = await list_evaluations(
        db, project_id=project.id, limit=limit, offset=offset
    )
    return PointCloudQualityEvaluationPage(items=rows, total=total)


@router.get(
    "/projects/{project_id}/point-cloud-quality/evaluations/{evaluation_id}",
    response_model=PointCloudQualityEvaluationOut,
)
async def get_project_evaluation(
    evaluation_id: uuid.UUID,
    project: Project = Depends(require_project_visible),
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
    _access: ProjectAccess = Depends(
        require_project_capability(ProjectCapability.REVIEW_WRITE.value)
    ),
):
    evaluation = await db.get(PointCloudQualityEvaluation, evaluation_id)
    if evaluation is None or evaluation.project_id != project.id:
        raise HTTPException(status_code=404, detail="Quality evaluation not found")
    return PointCloudQualityEvaluationOut.model_validate(evaluation)


@router.post(
    "/projects/{project_id}/point-cloud-quality/evaluations/{evaluation_id}/promote",
    response_model=PointCloudQualityEvaluationOut,
)
async def promote_project_evaluation(
    evaluation_id: uuid.UUID,
    request: Request,
    project: Project = Depends(require_project_visible),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _access: ProjectAccess = Depends(
        require_project_capability(ProjectCapability.PROJECT_MANAGE.value)
    ),
):
    fresh_access = await resolve_project_access(
        db, user=current_user, project=project, lock_membership=True
    )
    assert_capability(fresh_access, ProjectCapability.PROJECT_MANAGE.value)
    try:
        evaluation, locked_project = await promote_evaluation(
            db,
            project_id=project.id,
            evaluation_id=evaluation_id,
            actor_id=current_user.id,
        )
    except PointCloudQualityError as exc:
        _raise_quality_error(exc)
    await AuditService.log(
        db,
        actor=current_user,
        action=AuditAction.POINT_CLOUD_QUALITY_EVALUATION_PROMOTE,
        target_type="point_cloud_quality_evaluation",
        target_id=evaluation.id,
        request=request,
        status_code=200,
        detail={
            "project_id": str(locked_project.id),
            "from_revision": evaluation.baseline_config_revision,
            "to_revision": evaluation.promoted_config_revision,
            "candidate_config_digest": evaluation.candidate_config_digest,
        },
    )
    await db.commit()
    await db.refresh(evaluation)
    return PointCloudQualityEvaluationOut.model_validate(evaluation)
