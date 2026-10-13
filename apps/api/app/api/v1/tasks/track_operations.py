"""3D Scene 轨迹拆分 / 合并 API。"""

from __future__ import annotations

from collections import Counter
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.tasks._shared import (
    _assert_task_editable,
    _assert_task_visible,
    _load_task_or_404,
    _visible_task_ids,
    require_task_annotation_write,
)
from app.db.models.project import Project
from app.db.models.annotation import Annotation
from app.db.models.scene_track import (
    SceneTrack,
    SceneTrackInterval,
    SceneTrackOperation,
)
from app.db.models.task import Task
from app.db.models.user import User
from app.deps import get_db, get_current_user
from app.services.project_access import ProjectAccess
from app.schemas.track_operation import (
    TrackOperationCandidatesResponse,
    TrackOperationExecuteRequest,
    TrackOperationPreviewResponse,
    TrackOperationRequest,
    TrackOperationResult,
)
from app.schemas.scene_track import (
    SceneTrackCommandExecuteRequest,
    SceneTrackCommandPreviewOut,
    SceneTrackCommandRequest,
    SceneTrackCommandResultOut,
    SceneTrackDetailOut,
    SceneTrackDiagnosticReportOut,
    SceneTrackIntervalOut,
    SceneTrackMemberSummary,
    SceneTrackOperationListItemOut,
    SceneTrackOperationListOut,
    SceneTrackRevertRequest,
)
from app.services.audit import AuditAction, AuditService
from app.services.task_lock import TaskLockService
from app.services.track_operation import (
    MAX_CANDIDATE_SCAN,
    PreparedTrackOperation,
    list_structural_merge_candidates,
    prepare_track_operation,
    resolve_scene_track_context,
)
from app.services.scene_track_domain import diagnose_scene_tracks
from app.services.scene_track_command import (
    PreparedSceneTrackCommand,
    apply_scene_track_command,
    prepare_scene_track_command,
    preview_payload,
    request_digest,
    revert_scene_track_operation,
)


router = APIRouter()


def _operation_track_filter(track_id: str):
    response = SceneTrackOperation.response_json
    return or_(
        response["track_id"].as_string() == track_id,
        response["secondary_track_id"].as_string() == track_id,
        response["created_track_id"].as_string() == track_id,
    )


def _operation_task_ids(operation: SceneTrackOperation) -> set[uuid.UUID]:
    task_ids: set[uuid.UUID] = set()
    for snapshot in (operation.before_state, operation.after_state):
        for state in snapshot.get("tracks", {}).values():
            for member in state.get("members", []):
                task_id = member.get("task_id")
                if task_id:
                    task_ids.add(uuid.UUID(task_id))
    return task_ids


async def _filter_visible_operations(
    db: AsyncSession,
    *,
    project: Project,
    current_user: User,
    operations: list[SceneTrackOperation],
    access: ProjectAccess | None = None,
) -> list[SceneTrackOperation]:
    operation_tasks = {
        operation.id: _operation_task_ids(operation) for operation in operations
    }
    all_task_ids = set().union(*operation_tasks.values()) if operation_tasks else set()
    visible_task_ids = await _visible_task_ids(
        db, project, current_user, list(all_task_ids), access=access
    )
    return [
        operation
        for operation in operations
        if operation_tasks[operation.id]
        and operation_tasks[operation.id].issubset(visible_task_ids)
    ]


@router.get(
    "/{task_id}/scene-tracks/{track_id}",
    response_model=SceneTrackDetailOut,
)
async def get_scene_track_detail(
    task_id: uuid.UUID,
    track_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    anchor_task = await _load_task_or_404(db, task_id)
    await _assert_task_visible(db, anchor_task, current_user)
    context = await resolve_scene_track_context(db, anchor_task)
    track = (
        await db.execute(
            select(SceneTrack)
            .where(SceneTrack.project_id == context.project_id)
            .where(SceneTrack.scene_id == context.scene_id)
            .where(SceneTrack.track_id == track_id)
            .where(SceneTrack.retired_at.is_(None))
        )
    ).scalar_one_or_none()
    if track is None:
        raise _track_error(404, "track_not_found", "3D Scene Track was not found")
    intervals = list(
        (
            await db.execute(
                select(SceneTrackInterval)
                .where(SceneTrackInterval.scene_track_id == track.id)
                .order_by(SceneTrackInterval.start_frame)
            )
        ).scalars()
    )
    members = list(
        (
            await db.execute(
                select(Annotation)
                .where(Annotation.scene_track_id == track.id)
                .where(Annotation.is_active.is_(True))
                .where(Annotation.was_cancelled.is_(False))
                .order_by(Annotation.task_id, Annotation.id)
            )
        ).scalars()
    )
    member_task_ids = {member.task_id for member in members}
    project = await db.get(Project, anchor_task.project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Task not found")
    visible_tasks = await _task_access_map(
        db,
        project=project,
        current_user=current_user,
        task_ids=member_task_ids,
    )
    if not member_task_ids.issubset(visible_tasks):
        raise _track_error(
            409,
            "track_member_unavailable",
            "every track member task must be visible",
        )

    frames_by_role: dict[str, list[int]] = {
        "keyframe": [],
        "derived": [],
        "sample": [],
    }
    for member in members:
        frame = context.task_to_frame.get(member.task_id)
        if frame is not None:
            frames_by_role.setdefault(member.temporal_role, []).append(frame)
    for frames in frames_by_role.values():
        frames.sort()
    role_counts = dict(sorted(Counter(row.temporal_role for row in members).items()))
    source_counts = dict(sorted(Counter(row.source for row in members).items()))

    current = context.anchor_frame
    contains_current = any(
        interval.start_frame <= current
        and (interval.end_frame is None or current <= interval.end_frame)
        for interval in intervals
    )
    has_future_member = any(
        context.task_to_frame.get(member.task_id, -1) > current for member in members
    )
    commands: list[str] = ["merge"]
    if contains_current:
        commands.extend(["mark_absent", "terminate"])
        if has_future_member:
            commands.append("split")
    else:
        commands.append("resume")
    recent_operations = list(
        (
            await db.execute(
                select(SceneTrackOperation)
                .where(SceneTrackOperation.scene_id == context.scene_id)
                .where(_operation_track_filter(track_id))
                .where(SceneTrackOperation.status == "committed")
                .where(SceneTrackOperation.kind != "revert")
                .order_by(SceneTrackOperation.created_at.desc())
                .limit(20)
            )
        ).scalars()
    )
    recent_operations = await _filter_visible_operations(
        db,
        project=project,
        current_user=current_user,
        operations=recent_operations,
    )
    if recent_operations:
        commands.append("revert")

    return SceneTrackDetailOut(
        id=track.id,
        project_id=track.project_id,
        scene_id=track.scene_id,
        scene_name=context.scene_name,
        track_id=track.track_id,
        class_name=track.class_name,
        presence_mode=track.presence_mode,
        attributes=track.attributes or {},
        attributes_meta=track.attributes_meta or {},
        revision=track.revision,
        retired_at=track.retired_at,
        current_frame=current,
        intervals=[SceneTrackIntervalOut.model_validate(row) for row in intervals],
        members=SceneTrackMemberSummary(
            total=len(members),
            by_temporal_role=role_counts,
            by_source=source_counts,
            keyframe_frames=frames_by_role["keyframe"],
            derived_frames=frames_by_role["derived"],
            sample_frames=frames_by_role["sample"],
        ),
        available_commands=commands,
    )


@router.get(
    "/{task_id}/scene-track-diagnostics",
    response_model=SceneTrackDiagnosticReportOut,
)
async def get_scene_track_diagnostics(
    task_id: uuid.UUID,
    limit: int = Query(200, ge=1, le=1000),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    anchor_task = await _load_task_or_404(db, task_id)
    await _assert_task_visible(db, anchor_task, current_user)
    context = await resolve_scene_track_context(db, anchor_task)
    project = await db.get(Project, anchor_task.project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Task not found")
    scene_task_ids = set(context.task_to_frame)
    visible_task_ids = await _visible_task_ids(
        db, project, current_user, list(scene_task_ids)
    )
    if not scene_task_ids.issubset(visible_task_ids):
        raise _track_error(
            409,
            "track_member_unavailable",
            "every scene task must be visible for full-scene diagnostics",
        )
    report = await diagnose_scene_tracks(
        db,
        project_id=context.project_id,
        scene_id=context.scene_id,
        limit=limit,
    )
    return SceneTrackDiagnosticReportOut(
        scene_id=report.scene_id,
        track_count=report.track_count,
        linked_member_count=report.linked_member_count,
        issue_counts=report.issue_counts,
        issues=[issue.__dict__ for issue in report.issues],
        truncated=report.truncated,
    )


def _track_error(status_code: int, reason: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"reason": reason, "message": message},
    )


async def _task_access_map(
    db: AsyncSession,
    *,
    project: Project,
    current_user: User,
    task_ids: set[uuid.UUID],
    access: ProjectAccess | None = None,
) -> dict[uuid.UUID, Task]:
    rows = list(
        (
            await db.execute(
                select(Task).where(Task.id.in_(task_ids)).order_by(Task.id)
            )
        ).scalars()
    )
    by_id = {row.id: row for row in rows}
    visible_ids = await _visible_task_ids(
        db, project, current_user, list(task_ids), access=access
    )
    return {task_id: task for task_id, task in by_id.items() if task_id in visible_ids}


def _tasks_are_editable(
    tasks: dict[uuid.UUID, Task],
    *,
    task_ids: set[uuid.UUID] | frozenset[uuid.UUID],
    current_user: User,
    access: ProjectAccess | None = None,
) -> bool:
    if not set(task_ids).issubset(tasks):
        return False
    try:
        for task_id in sorted(task_ids, key=str):
            _assert_task_editable(tasks[task_id], current_user, access=access)
    except HTTPException:
        return False
    return True


async def _assert_prepared_mutable(
    db: AsyncSession,
    *,
    anchor_task: Task,
    current_user: User,
    prepared: PreparedTrackOperation | PreparedSceneTrackCommand,
    access: ProjectAccess | None = None,
) -> None:
    project = await db.get(Project, anchor_task.project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Task not found")
    task_ids = set(prepared.task_ids)
    tasks = await _task_access_map(
        db,
        project=project,
        current_user=current_user,
        task_ids=task_ids,
        access=access,
    )
    if not _tasks_are_editable(
        tasks,
        task_ids=task_ids,
        current_user=current_user,
        access=access,
    ):
        raise _track_error(
            409,
            "track_member_unavailable",
            "every track member task must be visible and editable",
        )


def _preview_response(prepared: PreparedTrackOperation):
    return {
        "operation": prepared.request.operation,
        "scene_id": prepared.context.scene_id,
        "scene_name": prepared.context.scene_name,
        "primary": prepared.primary.summary,
        "secondary": prepared.secondary.summary if prepared.secondary else None,
        "survivor_track_id": prepared.primary.summary.track_id,
        "affected_member_count": prepared.affected_member_count,
        "rewritten_member_count": prepared.rewritten_member_count,
        "snapshot_token": prepared.snapshot_token,
    }


def _compat_preview_response(
    prepared: PreparedTrackOperation,
    command: PreparedSceneTrackCommand,
):
    return {
        **_preview_response(prepared),
        "snapshot_token": command.snapshot_token,
    }


@router.post(
    "/{task_id}/scene-track-commands/preview",
    response_model=SceneTrackCommandPreviewOut,
)
async def preview_scene_track_command(
    task_id: uuid.UUID,
    data: SceneTrackCommandRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    access: ProjectAccess = Depends(require_task_annotation_write),
):
    anchor_task = await _load_task_or_404(db, task_id)
    await _assert_task_visible(db, anchor_task, current_user, access=access)
    _assert_task_editable(anchor_task, current_user, access=access)
    prepared = await prepare_scene_track_command(
        db,
        anchor_task=anchor_task,
        request=data,
    )
    await _assert_prepared_mutable(
        db,
        anchor_task=anchor_task,
        current_user=current_user,
        prepared=prepared,
        access=access,
    )
    return SceneTrackCommandPreviewOut(**preview_payload(prepared))


@router.post(
    "/{task_id}/scene-track-commands/execute",
    response_model=SceneTrackCommandResultOut,
)
async def execute_scene_track_command(
    task_id: uuid.UUID,
    data: SceneTrackCommandExecuteRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    access: ProjectAccess = Depends(require_task_annotation_write),
):
    anchor_task = await _load_task_or_404(db, task_id)
    await _assert_task_visible(db, anchor_task, current_user, access=access)
    _assert_task_editable(anchor_task, current_user, access=access)
    command = SceneTrackCommandRequest.model_validate(
        data.model_dump(exclude={"snapshot_token", "idempotency_key"})
    )
    context = await resolve_scene_track_context(db, anchor_task)
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {
            "key": (
                f"aap:scene-track-command:{context.scene_id}:"
                f"{current_user.id}:{data.idempotency_key}"
            )
        },
    )
    existing = (
        await db.execute(
            select(SceneTrackOperation)
            .where(SceneTrackOperation.scene_id == context.scene_id)
            .where(SceneTrackOperation.actor_id == current_user.id)
            .where(SceneTrackOperation.idempotency_key == data.idempotency_key)
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.request_digest != request_digest(command):
            raise _track_error(
                409,
                "idempotency_key_reused",
                "idempotency key was already used for a different command",
            )
        return SceneTrackCommandResultOut.model_validate(existing.response_json)
    prepared = await prepare_scene_track_command(
        db,
        anchor_task=anchor_task,
        request=command,
        for_update=True,
    )
    await _assert_prepared_mutable(
        db,
        anchor_task=anchor_task,
        current_user=current_user,
        prepared=prepared,
        access=access,
    )
    operation, response = await apply_scene_track_command(
        db,
        prepared=prepared,
        expected_snapshot_token=data.snapshot_token,
        actor_id=current_user.id,
        idempotency_key=data.idempotency_key,
    )
    await TaskLockService(db).heartbeat(anchor_task.id, current_user.id)
    await AuditService.log(
        db,
        actor=current_user,
        action=AuditAction.ANNOTATION_UPDATE,
        target_type="scene_track_operation",
        target_id=str(operation.id),
        request=request,
        status_code=200,
        detail={
            "operation": f"scene_track.{command.kind}",
            "scene_id": str(prepared.context.scene_id),
            "track_id": command.track_id,
            "secondary_track_id": command.secondary_track_id,
            "frame_index": command.frame_index,
            "resume_frame": command.resume_frame,
            "affected_member_count": len(prepared.affected_members),
        },
    )
    await db.commit()
    return SceneTrackCommandResultOut.model_validate(response)


@router.get(
    "/{task_id}/scene-track-commands",
    response_model=SceneTrackOperationListOut,
)
async def list_scene_track_operations(
    task_id: uuid.UUID,
    track_id: str = Query(..., min_length=1, max_length=64),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    anchor_task = await _load_task_or_404(db, task_id)
    await _assert_task_visible(db, anchor_task, current_user)
    context = await resolve_scene_track_context(db, anchor_task)
    project = await db.get(Project, anchor_task.project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Task not found")
    rows = list(
        (
            await db.execute(
                select(SceneTrackOperation)
                .where(SceneTrackOperation.scene_id == context.scene_id)
                .where(_operation_track_filter(track_id))
                .order_by(SceneTrackOperation.created_at.desc())
                .limit(limit * 4)
            )
        ).scalars()
    )
    matched = (
        await _filter_visible_operations(
            db,
            project=project,
            current_user=current_user,
            operations=rows,
        )
    )[:limit]
    return SceneTrackOperationListOut(
        operations=[
            SceneTrackOperationListItemOut(
                id=row.id,
                kind=row.kind,
                status=row.status,
                created_at=row.created_at,
                completed_at=row.completed_at,
                response=SceneTrackCommandResultOut.model_validate(row.response_json),
            )
            for row in matched
        ]
    )


@router.post(
    "/{task_id}/scene-track-commands/{operation_id}/revert",
    response_model=SceneTrackCommandResultOut,
)
async def revert_scene_track_command(
    task_id: uuid.UUID,
    operation_id: uuid.UUID,
    data: SceneTrackRevertRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    access: ProjectAccess = Depends(require_task_annotation_write),
):
    anchor_task = await _load_task_or_404(db, task_id)
    await _assert_task_visible(db, anchor_task, current_user, access=access)
    _assert_task_editable(anchor_task, current_user, access=access)
    context = await resolve_scene_track_context(db, anchor_task)
    operation = (
        await db.execute(
            select(SceneTrackOperation)
            .where(SceneTrackOperation.id == operation_id)
            .where(SceneTrackOperation.scene_id == context.scene_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if operation is None:
        raise _track_error(
            404, "operation_not_found", "Scene Track operation was not found"
        )

    task_ids = _operation_task_ids(operation) or {anchor_task.id}
    project = await db.get(Project, anchor_task.project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Task not found")
    tasks = await _task_access_map(
        db,
        project=project,
        current_user=current_user,
        task_ids=task_ids,
        access=access,
    )
    if not _tasks_are_editable(
        tasks, task_ids=task_ids, current_user=current_user, access=access
    ):
        raise _track_error(
            409,
            "track_member_unavailable",
            "every affected Track member task must be visible and editable",
        )

    revert_operation, response = await revert_scene_track_operation(
        db,
        operation=operation,
        actor_id=current_user.id,
        idempotency_key=data.idempotency_key,
    )
    await TaskLockService(db).heartbeat(anchor_task.id, current_user.id)
    await AuditService.log(
        db,
        actor=current_user,
        action=AuditAction.ANNOTATION_UPDATE,
        target_type="scene_track_operation",
        target_id=str(revert_operation.id),
        request=request,
        status_code=200,
        detail={
            "operation": "scene_track.revert",
            "reverted_operation_id": str(operation.id),
            "scene_id": str(context.scene_id),
        },
    )
    await db.commit()
    return SceneTrackCommandResultOut.model_validate(response)


@router.get(
    "/{task_id}/track-operations/candidates",
    response_model=TrackOperationCandidatesResponse,
)
async def list_track_operation_candidates(
    task_id: uuid.UUID,
    track_id: str = Query(..., min_length=1, max_length=64),
    limit: int = Query(20, ge=1, le=20),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    access: ProjectAccess = Depends(require_task_annotation_write),
):
    anchor_task = await _load_task_or_404(db, task_id)
    await _assert_task_visible(db, anchor_task, current_user, access=access)
    _assert_task_editable(anchor_task, current_user, access=access)
    _, structural = await list_structural_merge_candidates(
        db,
        anchor_task=anchor_task,
        primary_track_id=track_id,
        limit=min(MAX_CANDIDATE_SCAN, limit * 4),
    )
    project = await db.get(Project, anchor_task.project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Task not found")
    all_task_ids = set(structural.primary.task_ids)
    for candidate in structural.candidates:
        all_task_ids.update(candidate.task_ids)
    tasks = await _task_access_map(
        db,
        project=project,
        current_user=current_user,
        task_ids=all_task_ids,
        access=access,
    )
    if not _tasks_are_editable(
        tasks,
        task_ids=structural.primary.task_ids,
        current_user=current_user,
        access=access,
    ):
        raise _track_error(
            409,
            "track_member_unavailable",
            "every selected track member task must be visible and editable",
        )

    visible_candidates = [
        candidate
        for candidate in structural.candidates
        if _tasks_are_editable(
            tasks,
            task_ids=candidate.task_ids,
            current_user=current_user,
            access=access,
        )
    ]
    return TrackOperationCandidatesResponse(
        primary=structural.primary.summary,
        candidates=[candidate.summary for candidate in visible_candidates[:limit]],
        truncated=(
            structural.truncated
            or len(structural.candidates) > limit
            or len(visible_candidates) > limit
        ),
    )


@router.post(
    "/{task_id}/track-operations/preview",
    response_model=TrackOperationPreviewResponse,
)
async def preview_track_operation(
    task_id: uuid.UUID,
    data: TrackOperationRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    access: ProjectAccess = Depends(require_task_annotation_write),
):
    anchor_task = await _load_task_or_404(db, task_id)
    await _assert_task_visible(db, anchor_task, current_user, access=access)
    _assert_task_editable(anchor_task, current_user, access=access)
    prepared = await prepare_track_operation(
        db,
        anchor_task=anchor_task,
        request=data,
    )
    await _assert_prepared_mutable(
        db,
        anchor_task=anchor_task,
        current_user=current_user,
        prepared=prepared,
        access=access,
    )
    command = await prepare_scene_track_command(
        db,
        anchor_task=anchor_task,
        request=SceneTrackCommandRequest(
            kind=data.operation,
            track_id=data.primary_track_id,
            secondary_track_id=data.secondary_track_id,
            frame_index=data.split_after_frame,
        ),
    )
    await _assert_prepared_mutable(
        db,
        anchor_task=anchor_task,
        current_user=current_user,
        prepared=command,
        access=access,
    )
    return TrackOperationPreviewResponse(**_compat_preview_response(prepared, command))


@router.post(
    "/{task_id}/track-operations",
    response_model=TrackOperationResult,
)
async def execute_track_operation(
    task_id: uuid.UUID,
    data: TrackOperationExecuteRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    access: ProjectAccess = Depends(require_task_annotation_write),
):
    anchor_task = await _load_task_or_404(db, task_id)
    await _assert_task_visible(db, anchor_task, current_user, access=access)
    _assert_task_editable(anchor_task, current_user, access=access)
    operation = TrackOperationRequest.model_validate(
        data.model_dump(exclude={"snapshot_token"})
    )
    prepared = await prepare_track_operation(
        db,
        anchor_task=anchor_task,
        request=operation,
    )
    await _assert_prepared_mutable(
        db,
        anchor_task=anchor_task,
        current_user=current_user,
        prepared=prepared,
        access=access,
    )
    command = await prepare_scene_track_command(
        db,
        anchor_task=anchor_task,
        request=SceneTrackCommandRequest(
            kind=operation.operation,
            track_id=operation.primary_track_id,
            secondary_track_id=operation.secondary_track_id,
            frame_index=operation.split_after_frame,
        ),
        for_update=True,
    )
    await _assert_prepared_mutable(
        db,
        anchor_task=anchor_task,
        current_user=current_user,
        prepared=command,
        access=access,
    )
    command_operation, command_response = await apply_scene_track_command(
        db,
        prepared=command,
        expected_snapshot_token=data.snapshot_token,
        actor_id=current_user.id,
        idempotency_key=f"legacy-track-operation-{uuid.uuid4()}",
    )
    await TaskLockService(db).heartbeat(anchor_task.id, current_user.id)
    await AuditService.log(
        db,
        actor=current_user,
        action=AuditAction.ANNOTATION_UPDATE,
        target_type="scene",
        target_id=str(prepared.context.scene_id),
        request=request,
        status_code=200,
        detail={
            "operation": f"point_cloud_track.{operation.operation}",
            "anchor_task_id": str(anchor_task.id),
            "split_after_frame": operation.split_after_frame,
            "primary_track_id": operation.primary_track_id,
            "secondary_track_id": operation.secondary_track_id,
            "survivor_track_id": prepared.primary.summary.track_id,
            "created_track_id": command_response.get("created_track_id"),
            "updated_member_count": prepared.affected_member_count,
            "scene_track_operation_id": str(command_operation.id),
            "snapshot_token_prefix": command.snapshot_token[:12],
        },
    )
    await db.commit()
    return TrackOperationResult(
        **_compat_preview_response(prepared, command),
        created_track_id=command_response.get("created_track_id"),
        updated_member_count=prepared.affected_member_count,
    )
