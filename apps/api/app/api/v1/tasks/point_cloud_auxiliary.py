"""Authorized read access to immutable, source-bound model display geometry."""

import asyncio
import uuid

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.tasks._shared import _assert_task_visible, _load_task_or_404
from app.config import settings
from app.db.models.point_cloud_auxiliary import PointCloudAuxiliaryGeneration
from app.db.models.project import Project
from app.db.models.user import User
from app.deps import get_current_user, get_db, require_scopes
from app.schemas.point_cloud_auxiliary import (
    PointCloudAugmentationResponse,
    PointCloudAuxiliaryAssetRequest,
    PointCloudAuxiliaryAssetResponse,
    PointCloudAuxiliaryCamera,
    PointCloudEnhancementConfig,
)
from app.services.point_cloud_auxiliary import (
    AuxiliaryError,
    find_asset,
    resolve_task_inputs,
)
from app.services.point_cloud_auxiliary.publication import utcnow
from app.services.point_cloud_auxiliary.publication import verify_descriptor
from app.services.sensor_calibration import SensorCalibrationError
from app.services.storage import storage_service

router = APIRouter()


async def _resolve(db, task):
    project = await db.get(Project, task.project_id)
    if project is None or project.data_type != "lidar":
        return (
            PointCloudAugmentationResponse(
                task_id=task.id,
                enabled=False,
                status="not_available",
                reason="not_point_cloud",
            ),
            None,
            None,
        )
    config = PointCloudEnhancementConfig.model_validate(
        project.point_cloud_enhancement_config or {}
    )
    base = dict(task_id=task.id, enabled=True, profile=config.profile)
    if not settings.point_cloud_ai_enhancement_enabled or not config.enabled:
        return (
            PointCloudAugmentationResponse(
                **{**base, "enabled": False},
                status="disabled",
                reason="deployment_disabled"
                if not settings.point_cloud_ai_enhancement_enabled
                else "project_disabled",
            ),
            None,
            None,
        )
    try:
        inputs = await resolve_task_inputs(db, task, config.profile)
    except (AuxiliaryError, SensorCalibrationError) as exc:
        stale = exc.code in {
            "source_hash_mismatch",
            "source_size_mismatch",
            "coordinate_calibration_mismatch",
            "coordinate_axis_mismatch",
        }
        return (
            PointCloudAugmentationResponse(
                **base, status="stale" if stale else "not_available", reason=exc.code
            ),
            None,
            None,
        )
    base.update(
        axis_convention=inputs.axis_convention, input_digest=inputs.input_digest
    )
    asset = await find_asset(db, inputs)
    if asset is None:
        return (
            PointCloudAugmentationResponse(
                **base, status="missing", reason="not_prepared"
            ),
            None,
            None,
        )
    base["asset_id"] = asset.id
    active = (
        await db.get(PointCloudAuxiliaryGeneration, (asset.id, asset.active_generation))
        if asset.active_generation
        else None
    )
    if active is not None and active.status == "ready":
        base["generation"] = active.generation
        if active.input_digest != inputs.input_digest:
            return (
                PointCloudAugmentationResponse(
                    **base, status="stale", reason="source_changed"
                ),
                asset,
                None,
            )
        if not active.cameras or not all(
            await asyncio.gather(
                *(
                    asyncio.to_thread(verify_descriptor, camera)
                    for camera in active.cameras
                )
            )
        ):
            return (
                PointCloudAugmentationResponse(
                    **base,
                    status="failed",
                    reason="published_object_missing_or_changed",
                ),
                asset,
                None,
            )
        cameras = [
            PointCloudAuxiliaryCamera.model_validate(camera)
            for camera in active.cameras
        ]
        return (
            PointCloudAugmentationResponse(**base, status="ready", cameras=cameras),
            asset,
            active,
        )
    latest = (
        await db.execute(
            select(PointCloudAuxiliaryGeneration)
            .where(PointCloudAuxiliaryGeneration.asset_id == asset.id)
            .order_by(PointCloudAuxiliaryGeneration.generation.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if latest is None:
        return (
            PointCloudAugmentationResponse(
                **base, status="missing", reason="not_prepared"
            ),
            asset,
            None,
        )
    base["generation"] = latest.generation
    building = (
        latest.status == "building"
        and latest.lease_expires_at is not None
        and latest.lease_expires_at > utcnow()
    )
    return (
        PointCloudAugmentationResponse(
            **base,
            status="building" if building else "failed",
            reason=None
            if building
            else (latest.error_code or "publication_lease_expired"),
        ),
        asset,
        None,
    )


@router.get(
    "/{task_id}/point-cloud/augmentation",
    response_model=PointCloudAugmentationResponse,
    dependencies=[Depends(require_scopes("annotations:read"))],
)
async def get_point_cloud_augmentation(
    task_id: uuid.UUID,
    response: Response,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    task = await _load_task_or_404(db, task_id)
    await _assert_task_visible(db, task, current_user)
    response.headers["Cache-Control"] = "private, no-store"
    result, _, _ = await _resolve(db, task)
    return result


@router.post(
    "/{task_id}/point-cloud/augmentation/assets",
    response_model=PointCloudAuxiliaryAssetResponse,
    dependencies=[Depends(require_scopes("annotations:read"))],
)
async def create_point_cloud_augmentation_asset_url(
    task_id: uuid.UUID,
    payload: PointCloudAuxiliaryAssetRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    task = await _load_task_or_404(db, task_id)
    await _assert_task_visible(db, task, current_user)
    response.headers["Cache-Control"] = "private, no-store"
    result, asset, generation = await _resolve(db, task)
    if result.status != "ready" or asset is None or generation is None:
        raise HTTPException(
            status_code=409,
            detail={
                "reason": result.reason or "asset_not_ready",
                "status": result.status,
            },
        )
    if payload.asset_id != asset.id or payload.generation != generation.generation:
        raise HTTPException(status_code=409, detail={"reason": "generation_changed"})
    camera = next(
        (entry for entry in generation.cameras if entry["role"] == payload.camera_role),
        None,
    )
    if camera is None:
        raise HTTPException(status_code=404, detail={"reason": "camera_asset_missing"})
    url = storage_service.generate_download_url(
        camera["key"],
        expires_in=300,
        bucket=storage_service.datasets_bucket,
        align=False,
    )
    return PointCloudAuxiliaryAssetResponse(
        url=url,
        expires_in=300,
        point_count=camera["point_count"],
        sha256=camera["sha256"],
        generation=generation.generation,
        camera_role=payload.camera_role,
        axis_convention=result.axis_convention,
    )
