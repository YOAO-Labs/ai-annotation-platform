from __future__ import annotations

from typing import Literal
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import task_access
from app.db.models.point_cloud_quality import (
    PointCloudQualityEvaluation,
    PointCloudQualityIssue,
    PointCloudQualityRun,
)
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.user import User
from app.application.point_cloud_quality_promotion import (
    promote_point_cloud_quality_evaluation,
)
from app.services.quality_job_dispatch import QualityJobDispatcher
from app.deps import (
    get_quality_job_dispatcher,
    get_current_user,
    get_db,
    require_project_capability,
    require_project_visible,
    require_active_task_actor,
)
from aap_workspace_contract.access import (
    ProjectAccess,
    ProjectCapability,
)
from app.services.project_access import (
    assert_capability,
    get_workspace_project,
    resolve_project_access,
)
from aap_workspace.point_quality_configuration_values import (
    POINT_CLOUD_QUALITY_RULE_CODES,
)
from app.schemas.point_cloud_quality import (
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
    decide_issue,
    dispatch_quality_run,
    record_dispatch_failure,
    list_issues,
    refresh_issue_staleness,
)
from app.services.point_cloud_quality.evaluation import (
    create_evaluation,
    list_evaluations,
)
from app.services.identity_contracts import IdentityActorFacts
from app.services.labeling_spatial_quality import (
    quality_records,
    quality_row,
    spatial_quality_data,
)


from app.services.labeling_core_read_relations import lock_task_for_core_read

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
    user: User | IdentityActorFacts,
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
        facts = await spatial_quality_data(
            db,
            "read_quality_facts",
            project_id=project.id,
            annotation_ids=body.annotation_ids,
            task_ids=[],
            track_ids=[],
            run_ids=[],
        )
        rows = quality_records(facts.get("annotations"))
        if {row.id for row in rows} != set(body.annotation_ids):
            raise HTTPException(status_code=404, detail="3D annotation not found")
        task_ids = {row.task_id for row in rows}
    facts = await spatial_quality_data(
        db,
        "read_quality_facts",
        project_id=project.id,
        annotation_ids=[],
        task_ids=sorted(task_ids, key=str),
        track_ids=[],
        run_ids=[],
    )
    tasks = quality_records(facts.get("tasks"))
    if {row.id for row in tasks} != task_ids:
        raise HTTPException(status_code=404, detail="Task not found")
    for task in tasks:
        await task_access.assert_task_visible(db, task, user.id, access=access)


async def _assert_issue_visible(
    db: AsyncSession,
    issue: PointCloudQualityIssue,
    user: User | IdentityActorFacts,
    *,
    lock: bool = False,
) -> tuple[Project, ProjectAccess]:
    project = await get_workspace_project(
        db, actor_id=None, project_id=issue.project_id
    )
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    access = await resolve_project_access(
        db, actor_id=user.id, project=project, lock_membership=lock
    )
    if access.is_manager:
        return project, access
    if issue.task_id is None:
        raise HTTPException(status_code=403, detail="Quality issue not visible")
    facts = await spatial_quality_data(
        db,
        "read_quality_facts",
        project_id=issue.project_id,
        annotation_ids=[],
        task_ids=[issue.task_id],
        track_ids=[],
        run_ids=[],
    )
    tasks = quality_records(facts.get("tasks"))
    task = tasks[0] if tasks else None
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    await task_access.assert_task_visible(db, task, user.id, access=access)
    return project, access


async def _lock_task_nowait(db: AsyncSession, task_id: uuid.UUID) -> Task:
    try:
        task = await lock_task_for_core_read(
            db, task_id, nowait=True, populate_existing=True
        )
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
    current_user: IdentityActorFacts = Depends(get_current_user),
    quality_dispatcher: QualityJobDispatcher = Depends(get_quality_job_dispatcher),
    access: ProjectAccess = Depends(
        require_project_capability(ProjectCapability.REVIEW_WRITE.value)
    ),
):
    # Re-resolve current authority under a membership lock for the write path and
    # re-assert the capability on the fresh access.
    access = await resolve_project_access(
        db, actor_id=current_user.id, project=project, lock_membership=True
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
        run_id, job_id = run.id, job.id
        try:
            await dispatch_quality_run(
                db, run_id=run_id, async_job_id=job_id, dispatcher=quality_dispatcher
            )
        except PointCloudQualityError as exc:
            await record_dispatch_failure(db, run_id=run_id, error=str(exc.detail))
            await async_job_svc.mark_failed(db, job_id, error=str(exc.detail))
            await db.commit()
            raise HTTPException(
                status_code=exc.status_code,
                detail={
                    **exc.detail,
                    "run_id": str(run_id),
                    "async_job_id": str(job_id),
                },
            ) from exc
    run_data = await spatial_quality_data(db, "get_point_run", run_id=run.id)
    run = quality_row(PointCloudQualityRun, run_data["row"])
    return _run_out(run, reused=not created)


@router.get(
    "/projects/{project_id}/point-cloud-quality/runs/{run_id}",
    response_model=PointCloudQualityRunOut,
)
async def get_run(
    run_id: uuid.UUID,
    project: Project = Depends(require_project_visible),
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(get_current_user),
    access: ProjectAccess = Depends(
        require_project_capability(ProjectCapability.REVIEW_WRITE.value)
    ),
):
    data = await spatial_quality_data(db, "get_point_run", run_id=run_id)
    run = quality_row(PointCloudQualityRun, data["row"]) if data["row"] else None
    if run is None or run.project_id != project.id:
        raise HTTPException(status_code=404, detail="Point cloud quality run not found")
    if not access.is_manager:
        task_ids = [uuid.UUID(value) for value in run.scope_json.get("task_ids", [])]
        if await task_access.visible_task_ids(
            db, project, current_user.id, task_ids, access=access
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
    current_user: IdentityActorFacts = Depends(get_current_user),
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
        source = await spatial_quality_data(
            db, "read_point_source", project_id=project.id
        )
        all_task_ids = [row.id for row in quality_records(source.get("tasks"))]
        allowed_task_ids = await task_access.visible_task_ids(
            db, project, current_user.id, all_task_ids, access=access
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
    current_user: IdentityActorFacts = Depends(get_current_user),
):
    data = await spatial_quality_data(db, "get_point_issue", issue_id=issue_id)
    issue = quality_row(PointCloudQualityIssue, data["row"]) if data["row"] else None
    if issue is None:
        raise HTTPException(status_code=404, detail="Quality issue not found")
    _project, access = await _assert_issue_visible(db, issue, current_user)
    _assert_review_authority(access)
    await refresh_issue_staleness(db, issue)
    await db.flush()
    data = await spatial_quality_data(db, "get_point_issue", issue_id=issue_id)
    issue = quality_row(PointCloudQualityIssue, data["row"])
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
    current_user: IdentityActorFacts = Depends(get_current_user),
):
    data = await spatial_quality_data(db, "get_point_issue", issue_id=issue_id)
    issue = quality_row(PointCloudQualityIssue, data["row"]) if data["row"] else None
    if issue is None:
        raise HTTPException(status_code=404, detail="Quality issue not found")
    # Account-first: resolve the locked membership, then take bounded task and
    # resource locks so a busy row cannot block indefinitely.
    _project, access = await _assert_issue_visible(db, issue, current_user, lock=True)
    _assert_review_authority(access)
    expected_task_id = issue.task_id
    if expected_task_id is not None:
        facts = await spatial_quality_data(
            db,
            "read_quality_facts",
            project_id=issue.project_id,
            annotation_ids=[],
            task_ids=[expected_task_id],
            track_ids=[],
            run_ids=[],
        )
        if quality_records(facts.get("tasks")):
            await _lock_task_nowait(db, expected_task_id)
    try:
        data = await spatial_quality_data(db, "lock_point_issue", issue_id=issue_id)
        issue = (
            quality_row(PointCloudQualityIssue, data["row"]) if data["row"] else None
        )
        if issue is None:
            raise HTTPException(status_code=404, detail="Quality issue not found")
    except DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", None) in {"55P03", "40P01", "40001"}:
            raise HTTPException(
                status_code=409, detail={"reason": "point_cloud_quality_issue_busy"}
            ) from exc
        raise
    previous = issue.status
    previous_verdict = issue.review_verdict
    try:
        await decide_issue(
            db,
            issue=issue,
            expected_task_id=expected_task_id,
            actor_id=current_user.id,
            status=body.status,
            reason=body.reason,
            review_verdict=body.review_verdict,
            review_note=body.review_note,
        )
    except PointCloudQualityError as exc:
        _raise_quality_error(exc)
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
    data = await spatial_quality_data(db, "get_point_issue", issue_id=issue.id)
    issue = quality_row(PointCloudQualityIssue, data["row"])
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
    current_user: IdentityActorFacts = Depends(get_current_user),
    _access: ProjectAccess = Depends(
        require_project_capability(ProjectCapability.PROJECT_MANAGE.value)
    ),
):
    fresh_access = await resolve_project_access(
        db, actor_id=current_user.id, project=project, lock_membership=True
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
    data = await spatial_quality_data(
        db, "get_point_evaluation", evaluation_id=evaluation.id
    )
    evaluation = quality_row(PointCloudQualityEvaluation, data["row"])
    return PointCloudQualityEvaluationOut.model_validate(evaluation)


@router.get(
    "/projects/{project_id}/point-cloud-quality/evaluations",
    response_model=PointCloudQualityEvaluationPage,
)
async def get_project_evaluations(
    project: Project = Depends(require_project_visible),
    db: AsyncSession = Depends(get_db),
    _current_user: IdentityActorFacts = Depends(get_current_user),
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
    _current_user: IdentityActorFacts = Depends(get_current_user),
    _access: ProjectAccess = Depends(
        require_project_capability(ProjectCapability.REVIEW_WRITE.value)
    ),
):
    data = await spatial_quality_data(
        db, "get_point_evaluation", evaluation_id=evaluation_id
    )
    evaluation = (
        quality_row(PointCloudQualityEvaluation, data["row"]) if data["row"] else None
    )
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
    current_user: IdentityActorFacts = Depends(get_current_user),
    _access: ProjectAccess = Depends(
        require_project_capability(ProjectCapability.PROJECT_MANAGE.value)
    ),
):
    fresh_access = await resolve_project_access(
        db, actor_id=current_user.id, project=project, lock_membership=True
    )
    assert_capability(fresh_access, ProjectCapability.PROJECT_MANAGE.value)
    try:
        (
            evaluation,
            locked_project,
        ) = await promote_point_cloud_quality_evaluation(
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
    data = await spatial_quality_data(
        db, "get_point_evaluation", evaluation_id=evaluation.id
    )
    evaluation = quality_row(PointCloudQualityEvaluation, data["row"])
    return PointCloudQualityEvaluationOut.model_validate(evaluation)
