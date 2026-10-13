"""3D Scene 跨帧任务中心 API。"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.tasks._shared import (
    _assert_task_editable,
    _assert_task_visible,
    _load_task_or_404,
    _resolve_task_access,
    _visible_task_ids,
    require_task_annotation_write,
)
from app.db.models.async_job import AsyncJob
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.user import User
from app.deps import get_db, get_current_user
from app.services.project_access import ProjectAccess
from app.schemas.async_job import AsyncJobOut
from app.schemas.cross_frame_job import CrossFrameJobCreate, CrossFrameJobListResponse
from app.services import async_job as async_job_svc
from app.services.async_job_notify import notify_job_terminal
from app.services.cross_frame_job import (
    ACTIVE_STATUSES,
    CONTRACT_VERSION,
    JOB_KIND,
    load_source_annotations,
    retryable_targets,
    singleflight_key,
    snapshot_sources,
)
from app.services.scene import get_scene_frame_task_map, resolve_task_scene_frames


router = APIRouter()


async def _dispatch_job(db: AsyncSession, job: AsyncJob) -> AsyncJob:
    await db.commit()
    try:
        from app.workers.cross_frame_job import run_cross_frame_job

        queued = run_cross_frame_job.delay(str(job.id))
    except Exception as exc:
        await async_job_svc.mark_failed(
            db, job.id, error="cross-frame job dispatch failed"
        )
        await notify_job_terminal(db, job_id=job.id)
        await db.commit()
        raise HTTPException(
            status_code=503,
            detail="cross-frame worker is unavailable",
        ) from exc
    current = await db.get(AsyncJob, job.id)
    if current is None:
        raise HTTPException(status_code=500, detail="cross-frame job disappeared")
    if not current.celery_task_id:
        current.celery_task_id = str(queued.id)
        await db.commit()
        await db.refresh(current)
    return current


async def _create_job(
    db: AsyncSession,
    *,
    source_task: Task,
    current_user: User,
    data: CrossFrameJobCreate,
    access: ProjectAccess | None = None,
    target_frame_override: list[int] | None = None,
    source_ids_override: list[uuid.UUID] | None = None,
    parent_job_id: uuid.UUID | None = None,
) -> tuple[AsyncJob, bool]:
    scene_frame = (await resolve_task_scene_frames(db, [source_task.id]))[
        source_task.id
    ]
    if scene_frame.scene_id is None or scene_frame.frame_index is None:
        raise HTTPException(
            status_code=422, detail="source task is not part of a Scene frame"
        )
    current_frame = scene_frame.frame_index
    if data.direction == "forward" and data.start_frame <= current_frame:
        raise HTTPException(
            status_code=422, detail="forward range must start after current frame"
        )
    if data.direction == "backward" and data.end_frame >= current_frame:
        raise HTTPException(
            status_code=422, detail="backward range must end before current frame"
        )

    project = await db.get(Project, source_task.project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Task not found")
    frame_task_map = await get_scene_frame_task_map(db, scene_frame.scene_id)
    requested_frames = (
        list(target_frame_override)
        if target_frame_override is not None
        else list(range(data.start_frame, data.end_frame + 1))
    )
    requested_frames = sorted(
        set(requested_frames), reverse=data.direction == "backward"
    )
    candidate_task_ids = [
        frame_task_map[frame_index]
        for frame_index in requested_frames
        if frame_index in frame_task_map
    ]
    task_rows = list(
        (
            await db.execute(select(Task).where(Task.id.in_(candidate_task_ids)))
        ).scalars()
    )
    tasks_by_id = {
        row.id: row for row in task_rows if row.project_id == source_task.project_id
    }
    visible_ids = await _visible_task_ids(
        db, project, current_user, list(tasks_by_id), access=access
    )

    targets: list[dict] = []
    for frame_index in requested_frames:
        target_id = frame_task_map.get(frame_index)
        target = tasks_by_id.get(target_id) if target_id else None
        if target is None:
            targets.append(
                {
                    "frame_index": frame_index,
                    "task_id": None,
                    "preflight_state": "missing",
                }
            )
            continue
        if target.id not in visible_ids:
            targets.append(
                {
                    "frame_index": frame_index,
                    "task_id": None,
                    "preflight_state": "unavailable",
                }
            )
            continue
        try:
            _assert_task_editable(target, current_user, access=access)
        except HTTPException:
            targets.append(
                {
                    "frame_index": frame_index,
                    "task_id": None,
                    "preflight_state": "not_editable",
                }
            )
            continue
        targets.append(
            {
                "frame_index": frame_index,
                "task_id": str(target.id),
                "preflight_state": "ready",
            }
        )
    if not any(target["preflight_state"] == "ready" for target in targets):
        raise HTTPException(
            status_code=409, detail="range has no visible editable target frames"
        )

    source_rows = await load_source_annotations(
        db,
        source_task_id=source_task.id,
        scope="selected" if source_ids_override is not None else data.scope,
        annotation_ids=source_ids_override
        if source_ids_override is not None
        else data.annotation_ids,
    )
    payload = {
        "contract_version": CONTRACT_VERSION,
        "source_task_id": str(source_task.id),
        "source_frame": current_frame,
        "scene_id": str(scene_frame.scene_id),
        "scene_name": scene_frame.scene_name,
        "operation": data.operation,
        "scope": data.scope,
        "direction": data.direction,
        "start_frame": data.start_frame,
        "end_frame": data.end_frame,
        "conflict_policy": data.conflict_policy,
        "sources": snapshot_sources(source_rows),
        "targets": targets,
        "total_frames": len(targets),
        "parent_job_id": str(parent_job_id) if parent_job_id else None,
        "project_display_id": project.display_id,
        "project_name": project.name,
    }
    payload["singleflight_key"] = singleflight_key(payload)
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {
            "key": (
                f"aap:cross-frame-singleflight:{current_user.id}:"
                f"{payload['singleflight_key']}"
            )
        },
    )
    existing = (
        await db.execute(
            select(AsyncJob)
            .where(AsyncJob.kind == JOB_KIND)
            .where(AsyncJob.user_id == current_user.id)
            .where(AsyncJob.status.in_(ACTIVE_STATUSES))
            .where(
                AsyncJob.payload["singleflight_key"].astext
                == payload["singleflight_key"]
            )
            .order_by(AsyncJob.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing, False
    job = await async_job_svc.create_job(
        db,
        kind=JOB_KIND,
        user_id=current_user.id,
        project_id=source_task.project_id,
        payload=payload,
    )
    return job, True


@router.post(
    "/{task_id}/cross-frame-jobs",
    response_model=AsyncJobOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_cross_frame_job(
    task_id: uuid.UUID,
    data: CrossFrameJobCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    access: ProjectAccess = Depends(require_task_annotation_write),
):
    source_task = await _load_task_or_404(db, task_id)
    await _assert_task_visible(db, source_task, current_user, access=access)
    _assert_task_editable(source_task, current_user, access=access)
    job, created = await _create_job(
        db,
        source_task=source_task,
        current_user=current_user,
        data=data,
        access=access,
    )
    if created:
        job = await _dispatch_job(db, job)
    return AsyncJobOut.model_validate(job)


@router.get(
    "/{task_id}/cross-frame-jobs",
    response_model=CrossFrameJobListResponse,
)
async def list_cross_frame_jobs(
    task_id: uuid.UUID,
    limit: int = Query(20, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    task = await _load_task_or_404(db, task_id)
    await _assert_task_visible(db, task, current_user)
    scene_frame = (await resolve_task_scene_frames(db, [task.id]))[task.id]
    if scene_frame.scene_id is None:
        return CrossFrameJobListResponse(items=[], total=0)
    stmt = (
        select(AsyncJob)
        .where(AsyncJob.kind == JOB_KIND)
        .where(AsyncJob.user_id == current_user.id)
        .where(AsyncJob.payload["scene_id"].astext == str(scene_frame.scene_id))
        .order_by(AsyncJob.created_at.desc())
    )
    rows = list((await db.execute(stmt.limit(limit))).scalars())
    total = (
        await db.execute(
            select(func.count())
            .select_from(AsyncJob)
            .where(AsyncJob.kind == JOB_KIND)
            .where(AsyncJob.user_id == current_user.id)
            .where(AsyncJob.payload["scene_id"].astext == str(scene_frame.scene_id))
        )
    ).scalar_one()
    return CrossFrameJobListResponse(
        items=[AsyncJobOut.model_validate(row) for row in rows], total=total
    )


@router.post(
    "/{task_id}/cross-frame-jobs/{job_id}/retry-failed",
    response_model=AsyncJobOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def retry_cross_frame_job(
    task_id: uuid.UUID,
    job_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    access: ProjectAccess = Depends(require_task_annotation_write),
):
    anchor_task = await _load_task_or_404(db, task_id)
    await _assert_task_visible(db, anchor_task, current_user, access=access)
    job = await db.get(AsyncJob, job_id)
    if job is None or job.kind != JOB_KIND:
        raise HTTPException(status_code=404, detail="cross-frame job not found")
    if job.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="not your job")
    if job.status not in {"completed", "failed", "cancelled"}:
        raise HTTPException(status_code=409, detail="active job cannot be retried")
    payload = job.payload or {}
    anchor_scene = (await resolve_task_scene_frames(db, [anchor_task.id]))[
        anchor_task.id
    ]
    if str(anchor_scene.scene_id) != str(payload.get("scene_id")):
        raise HTTPException(status_code=404, detail="cross-frame job not found")
    targets = retryable_targets(job.result or {})
    if not targets:
        raise HTTPException(
            status_code=409, detail="job has no retryable failed frames"
        )
    try:
        source_task_id = uuid.UUID(str(payload["source_task_id"]))
        source_ids = [
            uuid.UUID(str(source["annotation_id"]))
            for source in payload.get("sources") or []
        ]
        frames = [int(target["frame_index"]) for target in targets]
        data = CrossFrameJobCreate(
            operation="propagate",
            scope=str(payload["scope"]),
            annotation_ids=[] if payload["scope"] == "all" else source_ids,
            direction=str(payload["direction"]),
            start_frame=min(frames),
            end_frame=max(frames),
            conflict_policy="skip_existing",
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=409, detail="job snapshot is not retryable"
        ) from exc
    source_task = await _load_task_or_404(db, source_task_id)
    source_access = await _resolve_task_access(db, source_task, current_user)
    await _assert_task_visible(db, source_task, current_user, access=source_access)
    _assert_task_editable(source_task, current_user, access=source_access)
    retry_job, created = await _create_job(
        db,
        source_task=source_task,
        current_user=current_user,
        data=data,
        access=source_access,
        target_frame_override=frames,
        source_ids_override=source_ids,
        parent_job_id=job.id,
    )
    if created:
        retry_job = await _dispatch_job(db, retry_job)
    return AsyncJobOut.model_validate(retry_job)
