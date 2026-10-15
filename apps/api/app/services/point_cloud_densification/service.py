"""Durable, project-authorized preparation requests for auxiliary point clouds."""

from __future__ import annotations

import hashlib
import json
import uuid

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models.async_job import AsyncJob, AsyncJobKind
from app.db.models.dataset import DatasetItem, Scene
from app.db.models.ml_backend_pool import MLBackendServicePool
from app.db.models.ml_backend_registry import ProjectMLBackendPool
from app.db.models.project import Project
from app.db.models.task import Task
from app.schemas.point_cloud_densification import (
    GENERATION_PROFILE,
    PointCloudDensificationCapability,
    PointCloudDensificationRequest,
)
from app.services import async_job as jobs
from app.services.point_cloud_auxiliary import resolve_task_inputs
from app.services.sensor_calibration import SensorCalibrationError
from app.services.point_cloud_auxiliary import AuxiliaryError


def logical_input_digest(inputs) -> str:
    """Freeze database identity separately from worker-verified storage versions."""
    manifest = inputs.dump_manifest()
    manifest.pop("input_digest", None)
    manifest["primary"].pop("fingerprint", None)
    for camera in manifest["cameras"]:
        camera["item"].pop("fingerprint", None)
    return hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


async def configured_pool(
    db: AsyncSession, project: Project, *, lock: bool = False
) -> MLBackendServicePool:
    config = project.point_cloud_enhancement_config or {}
    if not settings.point_cloud_ai_enhancement_enabled:
        raise HTTPException(409, detail={"reason": "deployment_disabled"})
    if project.data_type != "lidar" or not config.get("enabled"):
        raise HTTPException(409, detail={"reason": "project_disabled"})
    if config.get("profile") != GENERATION_PROFILE:
        raise HTTPException(409, detail={"reason": "generation_profile_required"})
    try:
        pool_id = uuid.UUID(str(config.get("ml_backend_pool_id")))
    except ValueError as exc:
        raise HTTPException(409, detail={"reason": "producer_pool_required"}) from exc
    query = (
        select(MLBackendServicePool)
        .join(
            ProjectMLBackendPool,
            ProjectMLBackendPool.pool_id == MLBackendServicePool.id,
        )
        .where(
            ProjectMLBackendPool.project_id == project.id,
            ProjectMLBackendPool.enabled.is_(True),
            MLBackendServicePool.id == pool_id,
            MLBackendServicePool.enabled.is_(True),
        )
    )
    if lock:
        query = query.with_for_update(
            read=True, nowait=True, of=[MLBackendServicePool, ProjectMLBackendPool]
        )
    pool = (await db.execute(query)).scalar_one_or_none()
    if pool is None or pool.legacy_instance_id is None:
        raise HTTPException(409, detail={"reason": "producer_pool_unavailable"})
    try:
        PointCloudDensificationCapability.model_validate(
            (pool.capability_snapshot or {}).get("point_cloud_densification")
        )
    except ValueError as exc:
        raise HTTPException(
            409, detail={"reason": "producer_capability_missing"}
        ) from exc
    return pool


async def scope_tasks(
    db: AsyncSession, project: Project, request: PointCloudDensificationRequest
) -> list[Task]:
    query = select(Task).where(
        Task.project_id == project.id, Task.file_type == "point_cloud"
    )
    if request.retry_job_id:
        previous = await db.get(AsyncJob, request.retry_job_id)
        if (
            previous is None
            or previous.project_id != project.id
            or previous.kind != AsyncJobKind.POINT_CLOUD_DENSIFICATION.value
        ):
            raise HTTPException(404, "Preparation job not found")
        if previous.status not in {"completed", "failed", "cancelled"}:
            raise HTTPException(409, "Preparation job is still active")
        failed = set((previous.result or {}).get("failed_task_ids", []))
        if request.scope != "task_ids" or not set(map(str, request.task_ids)).issubset(
            failed
        ):
            raise HTTPException(
                422, "Retry scope must contain failed tasks from this job"
            )
    if request.scope == "task_ids":
        query = query.where(Task.id.in_(request.task_ids)).order_by(Task.id)
    else:
        query = (
            query.join(DatasetItem, DatasetItem.id == Task.dataset_item_id)
            .where(DatasetItem.scene_id == request.scene_id)
            .order_by(DatasetItem.frame_index, Task.id)
        )
    tasks = list((await db.execute(query.limit(501))).scalars())
    if not tasks or (
        request.scope == "task_ids" and {t.id for t in tasks} != set(request.task_ids)
    ):
        raise HTTPException(404, "Point-cloud tasks not found in this project")
    if len(tasks) > 500:
        raise HTTPException(
            422, "Select at most 500 point-cloud frames per preparation job"
        )
    return tasks


async def create_preparation(
    db: AsyncSession,
    project: Project,
    actor_id: uuid.UUID,
    request: PointCloudDensificationRequest,
) -> tuple[AsyncJob, bool]:
    pool = await configured_pool(db, project)
    tasks = await scope_tasks(db, project, request)
    snapshots = {}
    for task in tasks:
        try:
            inputs = await resolve_task_inputs(
                db, task, GENERATION_PROFILE, verify_objects=False
            )
        except (AuxiliaryError, SensorCalibrationError) as exc:
            raise HTTPException(
                409, detail={"reason": exc.code, "task_id": str(task.id)}
            ) from exc
        if len(inputs.cameras) != 6:
            raise HTTPException(
                409,
                detail={
                    "reason": "six_current_cameras_required",
                    "task_id": str(task.id),
                },
            )
        snapshots[str(task.id)] = logical_input_digest(inputs)
    identity = {
        "actor_id": str(actor_id),
        "project_id": str(project.id),
        "pool_id": str(pool.id),
        "profile": GENERATION_PROFILE,
        "task_inputs": snapshots,
    }
    digest = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    lock = int(digest[:16], 16) & ((1 << 63) - 1)
    await db.execute(text("SELECT pg_advisory_xact_lock(:lock)"), {"lock": lock})
    existing = (
        await db.execute(
            select(AsyncJob)
            .where(
                AsyncJob.kind == AsyncJobKind.POINT_CLOUD_DENSIFICATION.value,
                AsyncJob.project_id == project.id,
                AsyncJob.status.in_(["pending", "running"]),
                AsyncJob.payload["request_digest"].astext == digest,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing:
        return existing, False
    job = await jobs.create_job(
        db,
        kind=AsyncJobKind.POINT_CLOUD_DENSIFICATION.value,
        user_id=actor_id,
        project_id=project.id,
        celery_task_id=str(uuid.uuid4()),
        payload={
            **identity,
            "request_digest": digest,
            "task_ids": [str(t.id) for t in tasks],
            "total_tasks": len(tasks),
            "scope": request.scope,
            "scene_id": str(request.scene_id) if request.scene_id else None,
            "project_name": project.name,
        },
    )
    return job, True


async def list_preparation_options(db: AsyncSession, project: Project) -> dict:
    pools = (
        await db.execute(
            select(MLBackendServicePool)
            .join(
                ProjectMLBackendPool,
                ProjectMLBackendPool.pool_id == MLBackendServicePool.id,
            )
            .where(
                ProjectMLBackendPool.project_id == project.id,
                ProjectMLBackendPool.enabled.is_(True),
                MLBackendServicePool.enabled.is_(True),
            )
            .order_by(MLBackendServicePool.name)
        )
    ).scalars()
    producers = []
    for pool in pools:
        try:
            capability = PointCloudDensificationCapability.model_validate(
                (pool.capability_snapshot or {}).get("point_cloud_densification")
            )
        except ValueError:
            continue
        producers.append(
            {
                "id": str(pool.id),
                "name": pool.name,
                "profile": capability.profiles[0].id,
            }
        )
    scenes = (
        await db.execute(
            select(Scene.id, Scene.name)
            .join(DatasetItem, DatasetItem.scene_id == Scene.id)
            .join(Task, Task.dataset_item_id == DatasetItem.id)
            .where(Task.project_id == project.id, Task.file_type == "point_cloud")
            .distinct()
            .order_by(Scene.name)
            .limit(500)
        )
    ).all()
    return {
        "deployment_enabled": settings.point_cloud_ai_enhancement_enabled,
        "producers": producers,
        "scenes": [{"id": str(s.id), "name": s.name} for s in scenes],
    }
