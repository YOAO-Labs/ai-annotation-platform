"""Prepare image retries with fresh authority and the original failed input."""

from dataclasses import dataclass
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select

from app.config import settings
from app.db.models.prediction import FailedPrediction
from app.db.models.prediction_execution import PredictionExecutionSnapshot
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.ml_backend_registry import MLBackendRegistry
from app.services import async_job as jobs
from app.services.content_snapshot import ContentError
from app.services.dataset_content import content_http_error
from app.services.prediction_execution import load_snapshot
from app.services.prediction_references import capture_authority
from app.services.reference_bindings import reference_binding
from app.services.reference_keys import is_reference


@dataclass(frozen=True)
class RetryDispatch:
    failed_id: UUID
    queue: str
    job_id: UUID | None = None

    def publish(self, actor_id):
        from app.workers.predictions_retry import retry_failed_prediction

        options = {"queue": self.queue}
        if self.job_id:
            options.update(
                task_id=str(self.job_id), kwargs={"async_job_id": str(self.job_id)}
            )
        return retry_failed_prediction.apply_async(
            args=[str(self.failed_id), str(actor_id)], **options
        )


async def prepare_retry(db, failure, actor, request, *, project_id=None):
    """Caller owns the failed-row claim. Commit every plan before publishing."""
    from app.api.v1.projects import _stage_device
    from app.services.pipeline_validation import resolve_preannotate_queue

    if project_id is not None and failure.project_id != project_id:
        raise HTTPException(409, detail="Failed prediction is outside this project")
    backend = (
        await db.get(MLBackendRegistry, failure.ml_backend_id)
        if failure.ml_backend_id
        else None
    )
    stored = (failure.extra or {}).get("request_context") or {}
    device = None
    if backend and isinstance(backend.health_meta, dict):
        profile = (backend.health_meta.get("capabilities") or {}).get(
            "resource_profile"
        ) or {}
        device = profile.get("device")
        from app.services.trained_models import controlled_device, is_controlled

        if is_controlled(stored.get("model_id")) or stored.get("frozen_model_ref"):
            device = controlled_device(backend)
    snapshot = digest = None
    devices = [device]
    if failure.execution_snapshot_id:
        snapshot, digest = await load_snapshot(
            db, failure.execution_snapshot_id, failure.project_id
        )
        devices = []
        for stage in snapshot["stages"]:
            stage_backend = await db.get(
                MLBackendRegistry, UUID(stage["ml_backend_id"])
            )
            devices.append(
                _stage_device(stage_backend, stage.get("model_id"))
                if stage_backend
                else None
            )
    queue = resolve_preannotate_queue(
        devices,
        gpu_queue=settings.preannotate_gpu_queue,
        cpu_queue=settings.preannotate_cpu_queue,
    )
    task = (
        await db.get(Task, failure.task_id, populate_existing=True)
        if failure.task_id
        else None
    )
    project = await db.get(Project, failure.project_id, populate_existing=True)
    reference_input = (failure.extra or {}).get("reference_input")
    original_input = (failure.extra or {}).get("original_input")
    if (
        not (project and project.data_type == "image")
        and not (task and task.file_type == "image")
        and not reference_input
        and not original_input
    ):
        return RetryDispatch(failure.id, queue)
    try:
        if not task or task.project_id != failure.project_id or not snapshot:
            raise ContentError("prediction_original_requires_new_request")
        if is_reference(task.file_path):
            if not isinstance(reference_input, dict) or original_input is not None:
                raise ContentError("reference_retry_requires_original_binding")
            current = await reference_binding(db, task)
            old = reference_input.get("reference") or {}
            if (
                reference_input.get("task_id") != str(task.id)
                or reference_input.get("item_id") != str(task.dataset_item_id)
                or any(
                    current.get(key)
                    != old.get(key, 1 if key == "maintenance_revision" else None)
                    for key in (
                        "dataset_id",
                        "asset_id",
                        "sha256",
                        "size",
                        "width",
                        "height",
                        "maintenance_revision",
                    )
                )
            ):
                raise ContentError("content_binding_changed")
        else:
            if not isinstance(original_input, dict) or reference_input is not None:
                raise ContentError("prediction_original_requires_new_request")
            from app.services.prediction_originals import managed_binding

            current = await managed_binding(db, task)
            if original_input.get("task_id") != str(
                task.id
            ) or current != original_input.get("original"):
                raise ContentError("content_binding_changed")
        from app.services.project_access import resolve_project_access

        if (
            project is None
            or not (
                await resolve_project_access(db, user=actor, project=project)
            ).is_manager
        ):
            raise HTTPException(403, detail="requires project admin")
        frozen = await db.get(
            PredictionExecutionSnapshot, failure.execution_snapshot_id
        )
        proof = await capture_authority(
            db,
            project,
            actor,
            request,
            [task],
            frozen,
            scope=snapshot["execution_scope"],
            batch_id=task.batch_id,
            predict_mode="append",
            execution_unit=snapshot.get("execution_unit"),
            job_kind="prediction_retry",
        )
        from app.workers.predictions_retry import _retry_job_metadata

        _, payload = await _retry_job_metadata(db, failure.id)
        payload["prediction_authority"] = proof
        job = await jobs.create_job(
            db,
            kind="prediction_retry",
            user_id=actor.id,
            project_id=project.id,
            payload=payload,
        )
        job.execution_snapshot_id = failure.execution_snapshot_id
        job.celery_task_id = str(job.id)
        failure.extra = {**failure.extra, "retry_job_id": str(job.id)}
        await db.flush()
        return RetryDispatch(failure.id, queue, job.id)
    except Exception as exc:
        error = content_http_error(exc)
        if (
            isinstance(error.detail, dict)
            and error.detail.get("code") == "prediction_original_requires_new_request"
        ):
            error.detail = {
                **error.detail,
                "message": "历史失败缺少可信原图或配置证明，请重新发起预标",
            }
        raise error from None


async def release_unpublished_retry(db, plan):
    from app.services.prediction import release_failed_prediction_retry_claim

    if plan.job_id is None:
        await release_failed_prediction_retry_claim(db, plan.failed_id)
        return
    job = await jobs.lock_job(db, plan.job_id)
    if job is None or job.status != "pending":
        return
    failure = await db.scalar(
        select(FailedPrediction)
        .where(FailedPrediction.id == plan.failed_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if failure and (failure.extra or {}).get("retry_job_id") == str(plan.job_id):
        failure.extra = {
            k: v
            for k, v in failure.extra.items()
            if k not in {"retry_job_id", "retry_pending"}
        }
    if job and job.status == "pending":
        await jobs.mark_failed(db, job.id, error="retry_dispatch_failed")
        from app.services.async_job_notify import notify_job_terminal

        await notify_job_terminal(db, job_id=job.id)


async def finish_retry_attempt(
    db, job_id, *, reason, cancelled=False, celery_task_id=None, pipeline_errors=None
):
    """One job -> failure lock order for normal errors and Celery fallbacks."""
    from datetime import datetime, timezone
    from app.services.async_job_notify import notify_job_terminal

    job = await jobs.lock_job(db, job_id)
    if (
        not job
        or job.kind != "prediction_retry"
        or not (job.payload or {}).get("prediction_authority")
        or (celery_task_id is not None and job.celery_task_id != celery_task_id)
    ):
        return None
    failed_id = UUID(job.payload["failed_prediction_id"])
    failure = await db.scalar(
        select(FailedPrediction)
        .where(FailedPrediction.id == failed_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    active = job.status in {"pending", "running"}
    if failure and (failure.extra or {}).get("retry_job_id") == str(job.id):
        extra = {
            k: v
            for k, v in failure.extra.items()
            if k not in {"retry_pending", "retry_job_id"}
        }
        if active:
            failure.retry_count = (failure.retry_count or 0) + 1
            failure.last_retry_at = datetime.now(timezone.utc)
            if pipeline_errors is not None:
                extra["last_pipeline_errors"] = pipeline_errors
        failure.extra = extra
    result = {
        "status": "cancelled" if cancelled else "failed",
        "failed_id": str(failed_id),
        "reason": reason,
        "success_count": 0,
        "failed_count": 1,
    }
    if active:
        if cancelled:
            await jobs.mark_cancelled(db, job.id, result=result)
        else:
            await jobs.mark_failed(db, job.id, error=reason, result=result)
        await notify_job_terminal(db, job_id=job.id)
    return job.result or result
