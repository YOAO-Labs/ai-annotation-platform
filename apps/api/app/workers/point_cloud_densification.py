"""CPU orchestration for a separately admitted current-frame VGGT producer."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import logging
import uuid

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models.async_job import AsyncJob, AsyncJobKind
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.user import User
from app.schemas.point_cloud_densification import GENERATION_PROFILE, INFERENCE_PROFILE
from app.services import async_job as jobs
from app.services.async_job_notify import notify_job_terminal
from app.services.gpu_arbitration.dispatch import build_gpu_dispatch_context_factory
from app.services.ml_routing.client import RoutedMLBackendClient
from app.services.point_cloud_auxiliary import (
    AuxiliaryError,
    fail_generation,
    publish_auxiliary,
    reserve_generation,
    resolve_task_inputs,
)
from app.services.point_cloud_densification.kernel import (
    build_clouds,
    load_array,
    prepare_camera,
)
from app.services.point_cloud_densification.service import (
    configured_pool,
    logical_input_digest,
)
from app.services.point_cloud_quality.kernel import parse_pcd_positions
from app.services.project_access import (
    ProjectCapability,
    assert_capability,
    resolve_project_access,
)
from app.services.storage import storage_service
from app.services.project_write_guard import lock_actor_project_authority
from app.workers._db import task_session
from app.workers.celery_app import celery_app

log = logging.getLogger(__name__)


class PreparationCancelled(Exception):
    pass


def read_object(
    key: str, *, limit: int, sha256: str | None = None, bucket: str | None = None
) -> bytes:
    response = storage_service.client.get_object(
        Bucket=bucket or storage_service.datasets_bucket, Key=key
    )
    body = response["Body"]
    try:
        payload = body.read(limit + 1)
    finally:
        body.close()
    if len(payload) > limit or (
        sha256 and hashlib.sha256(payload).hexdigest() != sha256
    ):
        raise AuxiliaryError("source_or_artifact_integrity_mismatch")
    return payload


async def check_authority(
    db, job_id: uuid.UUID, *, lock: bool = False, task_id: uuid.UUID | None = None
):
    job_query = (
        select(AsyncJob)
        .where(AsyncJob.id == job_id)
        .execution_options(populate_existing=True)
    )
    job = (await db.execute(job_query)).scalar_one_or_none()
    if (
        job is None
        or job.status not in {"pending", "running"}
        or (job.payload or {}).get("cancel_requested")
    ):
        raise PreparationCancelled()
    actor = (
        await db.get(User, job.user_id, populate_existing=True) if job.user_id else None
    )
    project = (
        await db.get(Project, job.project_id, populate_existing=True)
        if job.project_id
        else None
    )
    if actor is None or not actor.is_active or project is None:
        raise AuxiliaryError("preparation_authority_revoked")
    if lock:
        await lock_actor_project_authority(db, actor.id, project.id)
        # Match the publisher's Task -> Project order. NOWAIT avoids cycles
        # with concurrent task/configuration mutations after authority locks.
        if task_id:
            await db.execute(
                select(Task.id)
                .where(Task.id == task_id, Task.project_id == project.id)
                .with_for_update(nowait=True)
            )
        await db.execute(
            select(Project.id)
            .where(Project.id == project.id)
            .with_for_update(nowait=True)
        )
    access = await resolve_project_access(
        db, user=actor, project=project, lock_membership=lock
    )
    assert_capability(access, ProjectCapability.PROJECT_MANAGE.value)
    pool = await configured_pool(db, project, lock=lock)
    if str(pool.id) != job.payload["pool_id"]:
        raise AuxiliaryError("producer_binding_changed")
    if lock:
        job = (await db.execute(job_query.with_for_update())).scalar_one()
        if job.status not in {"pending", "running"} or job.payload.get(
            "cancel_requested"
        ):
            raise PreparationCancelled()
    return job, project, pool


def _error_code(exc: Exception) -> str:
    if isinstance(exc, AuxiliaryError):
        return exc.code
    if isinstance(exc, HTTPException):
        if isinstance(exc.detail, dict) and "error_code" in exc.detail:
            return "producer_request_failed"
        if isinstance(exc.detail, str) and exc.detail.startswith("ML backend:"):
            return (
                "producer_authentication_failed"
                if exc.status_code in {401, 403}
                else "producer_request_failed"
            )
        return (
            str(exc.detail.get("reason", "preparation_authority_revoked"))
            if isinstance(exc.detail, dict)
            else "preparation_authority_revoked"
        )
    # Backend messages may include private signed URLs. Persist bounded codes.
    return "producer_or_geometry_failed"


def _clean_staging(keys: list[str]) -> None:
    for key in keys:
        try:
            storage_service.delete_object(key, bucket=storage_service.import_bucket)
        except Exception:
            # The import bucket's lifecycle is the fallback for interrupted I/O.
            log.warning("point-cloud staging deletion deferred")


async def prepare_one(db, job_id: uuid.UUID, task_id: uuid.UUID) -> str:
    staging, reserved = [], None
    try:
        job, _, pool = await check_authority(db, job_id)
        task = await db.get(Task, task_id, populate_existing=True)
        if task is None or task.project_id != job.project_id:
            raise AuxiliaryError("task_binding_changed")
        inputs = await resolve_task_inputs(db, task, GENERATION_PROFILE)
        if logical_input_digest(inputs) != job.payload["task_inputs"][str(task_id)]:
            raise AuxiliaryError("source_changed_since_request")
        generation = await reserve_generation(db, task, inputs)
        if generation.status == "ready":
            await db.commit()
            return "skipped"
        reserved = generation.asset_id, generation.generation, generation.lease_token
        await db.commit()
        unit_id = str(uuid.uuid4())
        staging.extend(
            f"point-cloud-aux-staging/{job_id}/{task_id}/{unit_id}/{camera.role}/{artifact}.npy"
            for camera in inputs.cameras
            for artifact in ("depth", "confidence")
        )

        async def signed_payload():
            await check_authority(db, job_id)
            camera_requests, uploads = [], []
            for index, camera in enumerate(inputs.cameras):
                source_url = storage_service.generate_download_url(
                    camera.file_path,
                    expires_in=300,
                    align=False,
                    bucket=storage_service.datasets_bucket,
                )
                camera_requests.append(
                    {
                        "role": camera.role,
                        "image_url": storage_service.rewrite_host_for_ml_backend(
                            source_url
                        ),
                        "sha256": camera.item.content_hash,
                        "width": camera.width,
                        "height": camera.height,
                    }
                )
                targets = {"role": camera.role}
                for offset, artifact in enumerate(("depth", "confidence")):
                    target = storage_service.generate_upload_url(
                        staging[index * 2 + offset],
                        expires_in=300,
                        bucket=storage_service.import_bucket,
                    )
                    targets[artifact] = {
                        "url": storage_service.rewrite_host_for_ml_backend(target),
                        "headers": {"Content-Type": "application/octet-stream"},
                    }
                uploads.append(targets)
            return {
                "protocol_version": "1",
                "request_id": unit_id,
                "idempotency_key": hashlib.sha256(
                    # Retries use fresh staging URLs while retaining the same
                    # bounded producer cache identity for frozen inputs.
                    f"{INFERENCE_PROFILE}:{inputs.input_digest}".encode()
                ).hexdigest(),
                "input_digest": inputs.input_digest,
                "profile": INFERENCE_PROFILE,
                "cameras": camera_requests,
                "upload_targets": uploads,
            }

        factory = async_sessionmaker(db.bind, expire_on_commit=False)
        client = RoutedMLBackendClient(
            db,
            pool.legacy_instance_id,
            project_id=task.project_id,
            owner=f"point_cloud_densification:{job_id}",
            operation="point_cloud_densification",
            shadow_session_factory=factory,
            dispatch_context_factory=build_gpu_dispatch_context_factory(factory),
        )
        result = await client.reconstruct(signed_payload)
        # Release routing reads before bounded CPU work. A later transaction
        # rechecks the actor, cancellation, configuration and every source.
        await db.commit()
        cameras = []
        for index, (camera, prediction) in enumerate(
            zip(inputs.cameras, result.cameras, strict=True)
        ):
            depth_bytes = await asyncio.to_thread(
                read_object,
                staging[index * 2],
                limit=2 * 1024 * 1024,
                bucket=storage_service.import_bucket,
            )
            confidence_bytes = await asyncio.to_thread(
                read_object,
                staging[index * 2 + 1],
                limit=2 * 1024 * 1024,
                bucket=storage_service.import_bucket,
            )
            depth, confidence = (
                load_array(depth_bytes, prediction.depth),
                load_array(confidence_bytes, prediction.confidence),
            )
            source = await asyncio.to_thread(
                read_object,
                camera.file_path,
                limit=16 * 1024 * 1024,
                sha256=camera.item.content_hash,
            )
            cameras.append(
                await asyncio.to_thread(
                    prepare_camera, source, camera, prediction, depth, confidence
                )
            )
        measured_bytes = await asyncio.to_thread(
            read_object,
            inputs.primary.file_path,
            limit=256 * 1024 * 1024,
            sha256=inputs.primary.content_hash,
        )
        measured = await asyncio.to_thread(parse_pcd_positions, measured_bytes)
        clouds, diagnostics = await asyncio.to_thread(build_clouds, measured, cameras)
        await check_authority(db, job_id, lock=True, task_id=task_id)
        provenance = {
            "producer": "vggt-omega-container",
            "profile": GENERATION_PROFILE,
            "model_source_commit": result.producer.source_commit,
            "weight_sha256": result.producer.weights_sha256,
            "preprocessing": {
                camera.role: camera.preprocessing.model_dump(mode="json")
                for camera in result.cameras
            },
            "filtering": {
                "confidence_quantile": 0.2,
                "depth_range_m": [1, 80],
                "bottom_fraction_removed": 0.05,
                "measured_ray_protection_px": 3,
                "voxel_m": 0.1,
                "diagnostics": diagnostics,
            },
            "calibration_policy": "full-current-lidar; no holdout evaluation",
            "timings": result.timings.model_dump(mode="json"),
        }
        # Cancelling takes the same job lock. A late request cannot overtake
        # this publication, and an earlier request cannot publish an asset.
        await publish_auxiliary(
            db, task, inputs, clouds, provenance, generation=generation
        )
        await db.commit()
        return "success"
    except Exception as exc:
        await db.rollback()
        if reserved and reserved[2]:
            await fail_generation(
                db,
                reserved[0],
                reserved[1],
                lease_token=reserved[2],
                reason="cancelled"
                if isinstance(exc, PreparationCancelled)
                else _error_code(exc),
            )
            await db.commit()
        raise
    finally:
        await asyncio.to_thread(_clean_staging, staging)


@celery_app.task(bind=True, name="app.workers.point_cloud_densification.run")
def run_point_cloud_densification(self, job_id: str) -> None:
    asyncio.run(_run(uuid.UUID(job_id), getattr(self.request, "id", None)))


async def _run(job_id: uuid.UUID, celery_task_id: str | None = None) -> None:
    async with task_session() as db:
        job = (
            await db.execute(
                select(AsyncJob).where(AsyncJob.id == job_id).with_for_update()
            )
        ).scalar_one_or_none()
        # Redelivery must never create concurrent physical calls for one job.
        if (
            job is None
            or job.kind != AsyncJobKind.POINT_CLOUD_DENSIFICATION.value
            or job.status != "pending"
        ):
            return
        task_ids = list(job.payload["task_ids"])
        await jobs.mark_running(db, job_id, celery_task_id=celery_task_id)
        await db.commit()
        summary = {
            "success_count": 0,
            "skipped_count": 0,
            "failed_count": 0,
            "failed_task_ids": [],
            "failures": [],
        }
        cancelled = False
        for index, task_id in enumerate(task_ids):
            try:
                outcome = await prepare_one(db, job_id, uuid.UUID(task_id))
                summary[f"{outcome}_count"] += 1
            except PreparationCancelled:
                cancelled = True
                break
            except Exception as exc:
                await db.rollback()
                summary["failed_count"] += 1
                summary["failed_task_ids"].append(task_id)
                summary["failures"].append(
                    {"task_id": task_id, "reason": _error_code(exc)}
                )
                log.warning(
                    "point-cloud preparation failed job=%s task=%s code=%s",
                    job_id,
                    task_id,
                    _error_code(exc),
                )
            # Serialize progress with cooperative cancel; never merge an old
            # payload over the user's cancellation request.
            job = (
                await db.execute(
                    select(AsyncJob)
                    .where(AsyncJob.id == job_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalar_one()
            job.result = dict(summary)
            job.progress_pct = int(100 * (index + 1) / len(task_ids))
            job.payload = {
                **job.payload,
                "heartbeat_at": datetime.now(timezone.utc).isoformat(),
            }
            await db.commit()
        job = (
            await db.execute(
                select(AsyncJob)
                .where(AsyncJob.id == job_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
        cancelled = cancelled or bool(job.payload.get("cancel_requested"))
        if cancelled:
            await jobs.mark_cancelled(db, job_id, result=summary)
        elif summary["failed_count"] == len(task_ids):
            await jobs.mark_failed(
                db, job_id, error="all_preparation_frames_failed", result=summary
            )
        else:
            await jobs.mark_complete(db, job_id, result=summary)
        await notify_job_terminal(db, job_id=job_id)
        await db.commit()


@celery_app.task(name="app.workers.point_cloud_densification.reconcile")
def reconcile_preparations() -> dict:
    return asyncio.run(_reconcile())


async def _reconcile() -> dict:
    # No live call may last this long: producer timeout is bounded and each
    # frame writes progress. Expired jobs retain retryable scope and evidence.
    cutoff = datetime.now(timezone.utc) - timedelta(hours=2)
    async with task_session() as db:
        rows = (
            await db.execute(
                select(AsyncJob)
                .where(
                    AsyncJob.kind == AsyncJobKind.POINT_CLOUD_DENSIFICATION.value,
                    AsyncJob.status.in_(["pending", "running"]),
                    AsyncJob.updated_at < cutoff,
                )
                .with_for_update(skip_locked=True)
                .limit(100)
            )
        ).scalars()
        count = 0
        for job in rows:
            result = dict(job.result or {})
            completed = set(result.get("failed_task_ids", []))
            result["failed_task_ids"] = sorted(
                completed | set(job.payload.get("task_ids", []))
            )
            await jobs.mark_failed(
                db, job.id, error="preparation_interrupted", result=result
            )
            await notify_job_terminal(db, job_id=job.id)
            count += 1
        await db.commit()
    return {"failed": count}
