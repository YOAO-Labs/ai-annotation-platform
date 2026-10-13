from __future__ import annotations

from typing import NoReturn
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.tasks._shared import (
    _assert_task_editable,
    _assert_task_visible,
    _load_task_or_404,
    require_task_annotation_write,
    require_task_annotation_write_strict,
)
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.task_dataset_item_link import TaskDatasetItemLink
from app.db.models.user import User
from app.deps import get_current_user, get_db, require_scopes
from app.schemas.multicamera_annotation import (
    CameraAnnotationMemberCreate,
    CameraAnnotationMemberDelete,
    CameraAnnotationMemberList,
    CameraAnnotationMemberOut,
    CameraAnnotationMemberRestore,
    CameraAnnotationMemberUpdate,
    SensorCalibrationHistoryOut,
    SensorCalibrationRevisionOut,
    SensorCalibrationUpdate,
)
from app.services.audit import AuditAction, AuditService
from app.services.multicamera_annotation import (
    MulticameraAnnotationError,
    camera_member_out,
    create_camera_member,
    delete_camera_member,
    list_camera_members,
    load_linked_camera_item,
    restore_camera_member,
    update_camera_member,
)
from app.services.project_access import ProjectAccess
from app.services.sensor_calibration import (
    SensorCalibrationError,
    list_calibration_revisions,
    update_calibration,
)


router = APIRouter()


def _raise_domain_error(exc: ValueError) -> NoReturn:
    code = getattr(exc, "code", "multicamera_annotation_invalid")
    status = 404 if code.endswith("_missing") else 409
    if code in {
        "camera_role_not_linked",
        "source_box_invalid",
        "camera_dimensions_missing",
    }:
        status = 422
    raise HTTPException(
        status_code=status,
        detail={"reason": code, "message": str(exc)},
    ) from exc


@router.get(
    "/{task_id}/point-cloud/camera-members",
    response_model=CameraAnnotationMemberList,
)
async def get_camera_members(
    task_id: uuid.UUID,
    scene_track_id: uuid.UUID | None = None,
    projection_camera_role: str | None = Query(default=None, pattern=r"^camera_"),
    include_inactive: bool = Query(False),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    task = await _load_task_or_404(db, task_id)
    await _assert_task_visible(db, task, current_user)
    try:
        items, track_revision, projected_bbox = await list_camera_members(
            db,
            task=task,
            scene_track_id=scene_track_id,
            include_inactive=include_inactive,
            projection_camera_role=projection_camera_role,
        )
    except (MulticameraAnnotationError, SensorCalibrationError) as exc:
        _raise_domain_error(exc)
    return CameraAnnotationMemberList(
        items=items,
        track_revision=track_revision,
        projected_bbox=projected_bbox,
    )


@router.post(
    "/{task_id}/point-cloud/camera-members",
    response_model=CameraAnnotationMemberOut,
    status_code=201,
    dependencies=[Depends(require_scopes("annotations:write"))],
)
async def post_camera_member(
    task_id: uuid.UUID,
    payload: CameraAnnotationMemberCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    access: ProjectAccess = Depends(require_task_annotation_write),
):
    task = await _load_task_or_404(db, task_id)
    await _assert_task_visible(db, task, current_user, access=access)
    _assert_task_editable(task, current_user, access=access)
    try:
        member = await create_camera_member(
            db,
            task=task,
            actor_id=current_user.id,
            source_annotation_id=payload.source_annotation_id,
            camera_role=payload.camera_role,
            bbox=payload.bbox,
            visibility=payload.visibility,
            expected_track_revision=payload.expected_track_revision,
            expected_calibration_revision=payload.expected_calibration_revision,
            expected_calibration_digest=payload.expected_calibration_digest,
        )
        out = await camera_member_out(db, task=task, member=member)
    except (MulticameraAnnotationError, SensorCalibrationError) as exc:
        _raise_domain_error(exc)
    await AuditService.log(
        db,
        actor=current_user,
        action=AuditAction.ANNOTATION_CREATE,
        target_type="camera_annotation_member",
        target_id=member.id,
        request=request,
        status_code=201,
        detail={
            "task_id": str(task.id),
            "scene_track_id": str(member.scene_track_id),
            "camera_role": member.sensor_role,
        },
    )
    await db.commit()
    return out


@router.patch(
    "/{task_id}/point-cloud/camera-members/{member_id}",
    response_model=CameraAnnotationMemberOut,
    dependencies=[Depends(require_scopes("annotations:write"))],
)
async def patch_camera_member(
    task_id: uuid.UUID,
    member_id: uuid.UUID,
    payload: CameraAnnotationMemberUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    access: ProjectAccess = Depends(require_task_annotation_write),
):
    task = await _load_task_or_404(db, task_id)
    await _assert_task_visible(db, task, current_user, access=access)
    _assert_task_editable(task, current_user, access=access)
    try:
        member = await update_camera_member(
            db,
            task=task,
            actor_id=current_user.id,
            member_id=member_id,
            bbox=payload.bbox,
            visibility=payload.visibility,
            expected_version=payload.expected_version,
            expected_track_revision=payload.expected_track_revision,
            expected_calibration_revision=payload.expected_calibration_revision,
            expected_calibration_digest=payload.expected_calibration_digest,
        )
        out = await camera_member_out(db, task=task, member=member)
    except (MulticameraAnnotationError, SensorCalibrationError) as exc:
        _raise_domain_error(exc)
    await AuditService.log(
        db,
        actor=current_user,
        action=AuditAction.ANNOTATION_UPDATE,
        target_type="camera_annotation_member",
        target_id=member.id,
        request=request,
        status_code=200,
        detail={"task_id": str(task.id), "camera_role": member.sensor_role},
    )
    await db.commit()
    return out


@router.delete(
    "/{task_id}/point-cloud/camera-members/{member_id}",
    response_model=CameraAnnotationMemberOut,
    dependencies=[Depends(require_scopes("annotations:write"))],
)
async def remove_camera_member(
    task_id: uuid.UUID,
    member_id: uuid.UUID,
    payload: CameraAnnotationMemberDelete,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    access: ProjectAccess = Depends(require_task_annotation_write),
):
    task = await _load_task_or_404(db, task_id)
    await _assert_task_visible(db, task, current_user, access=access)
    _assert_task_editable(task, current_user, access=access)
    try:
        member = await delete_camera_member(
            db,
            task=task,
            actor_id=current_user.id,
            member_id=member_id,
            expected_version=payload.expected_version,
            expected_track_revision=payload.expected_track_revision,
        )
        out = await camera_member_out(db, task=task, member=member)
    except (MulticameraAnnotationError, SensorCalibrationError) as exc:
        _raise_domain_error(exc)
    await AuditService.log(
        db,
        actor=current_user,
        action=AuditAction.ANNOTATION_DELETE,
        target_type="camera_annotation_member",
        target_id=member.id,
        request=request,
        status_code=200,
        detail={"task_id": str(task.id), "camera_role": member.sensor_role},
    )
    await db.commit()
    return out


@router.post(
    "/{task_id}/point-cloud/camera-members/{member_id}/restore",
    response_model=CameraAnnotationMemberOut,
    dependencies=[Depends(require_scopes("annotations:write"))],
)
async def restore_deleted_camera_member(
    task_id: uuid.UUID,
    member_id: uuid.UUID,
    payload: CameraAnnotationMemberRestore,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    access: ProjectAccess = Depends(require_task_annotation_write),
):
    task = await _load_task_or_404(db, task_id)
    await _assert_task_visible(db, task, current_user, access=access)
    _assert_task_editable(task, current_user, access=access)
    try:
        member = await restore_camera_member(
            db,
            task=task,
            actor_id=current_user.id,
            member_id=member_id,
            expected_version=payload.expected_version,
            expected_track_revision=payload.expected_track_revision,
            expected_calibration_revision=payload.expected_calibration_revision,
            expected_calibration_digest=payload.expected_calibration_digest,
        )
        out = await camera_member_out(db, task=task, member=member)
    except (MulticameraAnnotationError, SensorCalibrationError) as exc:
        _raise_domain_error(exc)
    await AuditService.log(
        db,
        actor=current_user,
        action=AuditAction.ANNOTATION_UPDATE,
        target_type="camera_annotation_member",
        target_id=member.id,
        request=request,
        status_code=200,
        detail={
            "task_id": str(task.id),
            "camera_role": member.sensor_role,
            "restored": True,
        },
    )
    await db.commit()
    return out


@router.get(
    "/{task_id}/point-cloud/cameras/{camera_role}/calibration",
    response_model=SensorCalibrationHistoryOut,
)
async def get_camera_calibration_history(
    task_id: uuid.UUID,
    camera_role: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    task = await _load_task_or_404(db, task_id)
    await _assert_task_visible(db, task, current_user)
    try:
        item = await load_linked_camera_item(db, task=task, camera_role=camera_role)
        revisions = await list_calibration_revisions(db, item)
    except (MulticameraAnnotationError, SensorCalibrationError) as exc:
        _raise_domain_error(exc)
    current = revisions[0]
    return SensorCalibrationHistoryOut(
        current_revision=current.revision,
        current_digest=current.digest,
        items=[
            SensorCalibrationRevisionOut(
                dataset_item_id=revision.dataset_item_id,
                revision=revision.revision,
                digest=revision.digest,
                calibration=revision.calibration,
                created_at=revision.created_at,
            )
            for revision in revisions
        ],
    )


@router.patch(
    "/{task_id}/point-cloud/cameras/{camera_role}/calibration",
    response_model=SensorCalibrationRevisionOut,
    dependencies=[Depends(require_scopes("annotations:write"))],
)
async def patch_camera_calibration(
    task_id: uuid.UUID,
    camera_role: str,
    payload: SensorCalibrationUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    access: ProjectAccess = Depends(require_task_annotation_write_strict),
):
    task = await _load_task_or_404(db, task_id)
    await _assert_task_visible(db, task, current_user, access=access)
    project = await db.get(Project, task.project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    if not access.is_manager:
        raise HTTPException(
            status_code=403, detail="Only the project owner can update calibration"
        )
    try:
        item = await load_linked_camera_item(db, task=task, camera_role=camera_role)
    except (MulticameraAnnotationError, SensorCalibrationError) as exc:
        _raise_domain_error(exc)
    if not access.is_super_admin:
        cross_project_id = await db.scalar(
            select(Task.project_id)
            .join(TaskDatasetItemLink, TaskDatasetItemLink.task_id == Task.id)
            .where(TaskDatasetItemLink.dataset_item_id == item.id)
            .where(Task.project_id != task.project_id)
            .limit(1)
        )
        if cross_project_id is not None:
            raise HTTPException(
                status_code=403,
                detail="Only a super admin can update calibration shared across projects",
            )
    try:
        state = await update_calibration(
            db,
            dataset_item_id=item.id,
            calibration=payload.calibration,
            expected_revision=payload.expected_revision,
            expected_digest=payload.expected_digest,
            actor_id=current_user.id,
        )
    except (MulticameraAnnotationError, SensorCalibrationError) as exc:
        _raise_domain_error(exc)
    await AuditService.log(
        db,
        actor=current_user,
        action=AuditAction.SENSOR_CALIBRATION_UPDATE,
        target_type="dataset_item",
        target_id=item.id,
        request=request,
        status_code=200,
        detail={
            "task_id": str(task.id),
            "camera_role": camera_role,
            "revision": state.revision,
            "digest": state.digest,
        },
    )
    await db.commit()
    return SensorCalibrationRevisionOut(
        dataset_item_id=state.dataset_item_id,
        revision=state.revision,
        digest=state.digest,
        calibration=state.calibration,
        created_at=state.created_at,
    )
