"""Cancellation use case with caller-owned transaction and explicit execution control."""

import logging
import uuid

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.async_job_contracts import (
    AsyncJobOperationFailure,
    JobCancellationPort,
)
from app.services.project_access import get_workspace_project
from app.application.job_terminal import reconcile_domain_terminal
from app.application.task_creation_errors import raise_task_creation_http_failure
from app.db.models.async_job import AsyncJobStatus
from app.services import async_job as jobs, async_job_access
from app.services.identity_account_lock_data import IdentityAccountLockData
from app.services import cross_frame_job, mask_repair, prediction, sample_evaluations
from app.services import training_datasets
from app.services.async_job_notify import notify_job_terminal
from app.services.job_execution_control import JobExecutionControl
from app.services.mask_formats import service as mask_formats
from app.services.project_write_guard import lock_project_manager
from app.services.task_creation_contract import TaskCreationFailure
from app.services.task_creation_jobs import TaskCreationJobs
from app.services.training_dataset_cancellation_contract import (
    TrainingDatasetCancellationFailure,
    TrainingDatasetCancellationPort,
)

log = logging.getLogger(__name__)

CANCELLABLE_KINDS = {
    "create_tasks",
    "sample_evaluation",
    "training_dataset",
    "batch_predict",
    "predictions_import",
    "audit_archive",
    "dataset_import",
    "mask_qc",
    "mask_repair",
    "mask_format_import",
    "point_cloud_cross_frame",
    "point_cloud_quality",
}
_REVOKE_KINDS = {
    "mask_qc",
    "point_cloud_quality",
    "mask_repair",
    "mask_format_import",
    "batch_predict",
    "point_cloud_cross_frame",
}


class SqlJobCancellation(JobCancellationPort):
    """Bind cancellation workflow to its caller's SQL session and runtime port."""

    def __init__(
        self, db: AsyncSession, *, execution_control: JobExecutionControl
    ) -> None:
        self._db = db
        self._execution_control = execution_control

    async def cancel(self, *, job_id: uuid.UUID, actor_id: uuid.UUID) -> str:
        try:
            db = self._db
            job = await jobs.get_job(db, job_id)
            if job is None:
                raise HTTPException(status_code=404, detail="async_job not found")
            payload = job.payload if isinstance(job.payload, dict) else {}
            if job.kind == "dataset_import":
                from app.services.assets_import import (
                    DATASET_IMPORT_PROTOCOL,
                    DatasetImportCapabilities,
                )

                if payload.get("import_protocol") == DATASET_IMPORT_PROTOCOL:
                    state = await DatasetImportCapabilities(db).cancel(
                        actor_id=actor_id, job_id=job.id
                    )
                    return state.stage
            if not await async_job_access._can_access_job(
                db, job=job, actor_id=actor_id
            ):
                actor = await IdentityAccountLockData(db).read_actor_status(actor_id)
                if actor is None or not actor.is_active:
                    raise HTTPException(
                        status_code=401,
                        detail="账号已停用或不存在",
                        headers={"WWW-Authenticate": "Bearer"},
                    )
                raise HTTPException(status_code=403, detail="not your job")
            if job.kind not in CANCELLABLE_KINDS:
                raise HTTPException(
                    status_code=400, detail=f"kind={job.kind} not cancellable"
                )
            if job.status not in {
                AsyncJobStatus.PENDING.value,
                AsyncJobStatus.RUNNING.value,
            }:
                raise HTTPException(
                    status_code=409,
                    detail=f"cannot cancel terminal job (status={job.status})",
                )

            if job.kind == "create_tasks":
                try:
                    result = await TaskCreationJobs(db).cancel(actor_id, job.id)
                except TaskCreationFailure as exc:
                    raise_task_creation_http_failure(exc)
                return result.status

            if job.kind == "sample_evaluation":
                project = await get_workspace_project(db, project_id=job.project_id)
                if project is None:
                    raise HTTPException(status_code=404, detail="项目不存在")
                await lock_project_manager(db, actor_id, project.id)
                await sample_evaluations.request_job_cancel(db, job.id)
                return "cancel_requested"

            if job.kind == "training_dataset":
                cancellation: TrainingDatasetCancellationPort = (
                    training_datasets.SqlTrainingDatasetCancellation(
                        db, execution_control=self._execution_control
                    )
                )
                try:
                    await cancellation.request_cancel(
                        job_id=job.id,
                        project_id=job.project_id,
                        actor_id=actor_id,
                    )
                except TrainingDatasetCancellationFailure as exc:
                    raise AsyncJobOperationFailure(
                        exc.status_code,
                        exc.detail,
                        headers=dict(exc.headers) if exc.headers is not None else None,
                    ) from exc
                return "cancelled"

            if job.kind in _REVOKE_KINDS and job.celery_task_id:
                try:
                    self._execution_control.request_cancel(job.celery_task_id)
                except Exception:
                    log.exception("%s revoke failed job=%s", job.kind, job.id)

            if job.kind in {"mask_qc", "point_cloud_quality"}:
                await jobs.mark_cancelled(
                    db, job.id, result={"reason": "cancelled_by_user"}
                )
                await reconcile_domain_terminal(db, job, cancelled=True)
                await notify_job_terminal(db, job_id=job.id)
                return "cancelled"

            pending = job.status == AsyncJobStatus.PENDING.value
            if job.kind == "mask_repair":
                return await mask_repair.request_job_cancel(db, job.id, pending=pending)
            if job.kind == "mask_format_import":
                return await mask_formats.request_job_cancel(
                    db, job.id, pending=pending
                )

            if job.kind in {"batch_predict", "point_cloud_cross_frame"}:
                if pending:
                    result = (
                        prediction.cancelled_batch_result(job.payload or {})
                        if job.kind == "batch_predict"
                        else cross_frame_job.cancelled_result(job.payload or {})
                    )
                    await jobs.mark_cancelled(db, job.id, result=result)
                    await notify_job_terminal(db, job_id=job.id)
                    return "cancelled"
                await jobs.request_cancel(db, job.id)
                return "cancel_requested"

            await jobs.mark_cancelled(db, job.id)
            await notify_job_terminal(db, job_id=job.id)
            return "cancelled"
        except HTTPException as exc:
            raise AsyncJobOperationFailure(
                exc.status_code, exc.detail, headers=exc.headers
            ) from exc
