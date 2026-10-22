from __future__ import annotations

from typing import NoReturn
import uuid
from ipaddress import ip_address

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import task_access
from app.api.v1.tasks._shared import (
    _load_task_or_404,
    require_task_annotation_write,
    require_task_annotation_write_strict,
)
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
    restore_camera_member,
    update_camera_member,
)
from aap_workspace_contract.access import ProjectAccess
from app.services.assets_spatial import AssetsSpatialGateway, AssetsSpatialGatewayError
from aap_assets_catalog.spatial_contract import (
    ReadCameraTargetRequest,
    UpdateCalibrationRequest,
)
from aap_audit_contract import ProjectDeletionAuditContext
from app.middleware.request_id import request_id_var
from app.services.audit import audit_origin_from_request
from app.services.project_access import get_workspace_project
from app.services.identity_contracts import IdentityActorFacts


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
    current_user: IdentityActorFacts = Depends(get_current_user),
):
    task = await _load_task_or_404(db, task_id)
    await task_access.assert_task_visible(db, task, current_user.id)
    try:
        items, track_revision, projected_bbox = await list_camera_members(
            db,
            task=task,
            scene_track_id=scene_track_id,
            include_inactive=include_inactive,
            projection_camera_role=projection_camera_role,
        )
    except AssetsSpatialGatewayError:
        raise
    except MulticameraAnnotationError as exc:
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
    current_user: IdentityActorFacts = Depends(get_current_user),
    access: ProjectAccess = Depends(require_task_annotation_write),
):
    task = await _load_task_or_404(db, task_id)
    await task_access.assert_task_visible(db, task, current_user.id, access=access)
    task_access.assert_task_editable(task, current_user.id, access=access)
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
    except AssetsSpatialGatewayError:
        raise
    except MulticameraAnnotationError as exc:
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
    current_user: IdentityActorFacts = Depends(get_current_user),
    access: ProjectAccess = Depends(require_task_annotation_write),
):
    task = await _load_task_or_404(db, task_id)
    await task_access.assert_task_visible(db, task, current_user.id, access=access)
    task_access.assert_task_editable(task, current_user.id, access=access)
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
    except AssetsSpatialGatewayError:
        raise
    except MulticameraAnnotationError as exc:
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
    current_user: IdentityActorFacts = Depends(get_current_user),
    access: ProjectAccess = Depends(require_task_annotation_write),
):
    task = await _load_task_or_404(db, task_id)
    await task_access.assert_task_visible(db, task, current_user.id, access=access)
    task_access.assert_task_editable(task, current_user.id, access=access)
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
    except AssetsSpatialGatewayError:
        raise
    except MulticameraAnnotationError as exc:
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
    current_user: IdentityActorFacts = Depends(get_current_user),
    access: ProjectAccess = Depends(require_task_annotation_write),
):
    task = await _load_task_or_404(db, task_id)
    await task_access.assert_task_visible(db, task, current_user.id, access=access)
    task_access.assert_task_editable(task, current_user.id, access=access)
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
    except AssetsSpatialGatewayError:
        raise
    except MulticameraAnnotationError as exc:
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
    current_user: IdentityActorFacts = Depends(get_current_user),
):
    task = await _load_task_or_404(db, task_id)
    await task_access.assert_task_visible(db, task, current_user.id)
    try:
        target = await AssetsSpatialGateway(db).read_camera_target(
            ReadCameraTargetRequest(
                actor_id=current_user.id,
                task_id=task.id,
                camera_role=camera_role,
            )
        )
        revisions = await AssetsSpatialGateway(db).read_calibration_history(
            target.dataset_item_id
        )
    except AssetsSpatialGatewayError:
        raise
    except MulticameraAnnotationError as exc:
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
    responses={
        200: {
            "headers": {
                "Idempotency-Key": {
                    "description": "Reuse this UUID to replay the committed calibration update.",
                    "schema": {"type": "string", "format": "uuid"},
                }
            }
        }
    },
)
async def patch_camera_calibration(
    task_id: uuid.UUID,
    camera_role: str,
    payload: SensorCalibrationUpdate,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(get_current_user),
    access: ProjectAccess = Depends(require_task_annotation_write_strict),
    idempotency_key: uuid.UUID | None = Header(default=None, alias="Idempotency-Key"),
):
    task = await _load_task_or_404(db, task_id)
    await task_access.assert_task_visible(db, task, current_user.id, access=access)
    project = await get_workspace_project(db, actor_id=None, project_id=task.project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    if not access.is_manager:
        raise HTTPException(
            status_code=403, detail="Only the project owner can update calibration"
        )
    origin = audit_origin_from_request(request)
    try:
        ip = str(ip_address(origin.ip)) if origin.ip else None
    except ValueError:
        ip = None
    audit_context = ProjectDeletionAuditContext(
        method=origin.method,
        path=origin.path,
        ip=ip,
        request_id=request_id_var.get()[:36] or None,
    )
    try:
        gateway = AssetsSpatialGateway(db)
        result = await gateway.update_calibration(
            UpdateCalibrationRequest(
                operation_id=idempotency_key or uuid.uuid4(),
                actor_id=current_user.id,
                task_id=task.id,
                camera_role=camera_role,
                expected_revision=payload.expected_revision,
                expected_digest=payload.expected_digest,
                calibration=payload.calibration,
                audit_context=audit_context.model_dump(mode="json"),
            )
        )
    except AssetsSpatialGatewayError as exc:
        if exc.code == "camera_shared_across_projects":
            raise HTTPException(status_code=403, detail=exc.message) from exc
        raise
    except MulticameraAnnotationError as exc:
        _raise_domain_error(exc)
    state = result.state
    response.headers["Idempotency-Key"] = str(result.operation_id)
    return SensorCalibrationRevisionOut(
        dataset_item_id=state.dataset_item_id,
        revision=state.revision,
        digest=state.digest,
        calibration=state.calibration,
        created_at=state.created_at,
    )
