"""Celery registration and runtime adapter for batch prediction."""

from __future__ import annotations

import asyncio

from app.application.preannotation_contract import BatchPredictionRequest
from app.application.preannotation_execution import (
    PreannotationInputDeferred,
    run_preannotation,
)
from app.application.preannotation_results import PreannotationResultDeferred
from app.config import settings
from app.services import prediction_progress
from app.workers.celery_app import celery_app


async def _mark_job_failed(celery_task_id: str, error_message: str) -> bool:
    """Persist an uncaught Celery task failure to its durable async job."""
    from sqlalchemy.ext.asyncio import (
        AsyncSession,
        async_sessionmaker,
        create_async_engine,
    )

    from app.services import async_job as async_job_svc
    from app.services.async_job_notify import notify_job_terminal
    from app.services.jobs_lifecycle_data import job_lock_statement

    engine = create_async_engine(settings.database_url, echo=False)
    SessionLocal = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    try:
        async with SessionLocal() as db:
            job = await async_job_svc.find_by_celery_task_id(db, celery_task_id)
            if job is None:
                return False
            if job.kind != "batch_predict":
                return False
            job = (
                await db.execute(
                    job_lock_statement(job.id, mode="update", populate_existing=True)
                )
            ).scalar_one()
            if job.status not in {"pending", "running"}:
                return job.status == "failed"
            digest = (job.payload or {}).get("preannotation_request_digest")
            if isinstance(digest, str):
                # A broker task ID cannot prove the request digest or current
                # lease owner. Only the fenced workflow may terminalize a bound
                # Job, including a rejected delivery before its first claim.
                return False
            await async_job_svc.mark_failed(db, job.id, error=error_message)
            await notify_job_terminal(db, job_id=job.id)
            await db.commit()
            return True
    finally:
        await engine.dispose()


class _BatchPredictTask(celery_app.Task):
    """Publish task-level failures to progress and the durable async job."""

    def on_failure(self, exc, task_id, args, kwargs, einfo):  # noqa: ARG002
        project_id = kwargs.get("project_id") or (args[0] if args else None)
        error_message = f"{type(exc).__name__}: {exc}"
        try:
            failed = asyncio.run(_mark_job_failed(task_id, error_message))
        except Exception:
            failed = False
        if project_id and failed:
            try:
                prediction_progress.publish_progress(
                    str(project_id),
                    0,
                    0,
                    status="error",
                    error=error_message,
                    job_meta={"job_celery_task_id": task_id},
                )
            except Exception:
                pass


@celery_app.task(
    bind=True,
    base=_BatchPredictTask,
    # Input readiness and saved-result contention defer the same durable Job.
    # The wait can exceed Celery's default three retries without losing output.
    max_retries=None,
    default_retry_delay=30,
    acks_late=True,
    reject_on_worker_lost=True,
)
def batch_predict(
    self,
    project_id: str,
    ml_backend_id: str,
    task_ids: list[str] | None = None,
    prompt: str | None = None,
    output_mode: str = "mask",
    batch_id: str | None = None,
    user_id: str | None = None,
    params: dict | None = None,
    predict_mode: str = "skip_predicted",
    model_id: str | None = None,
    task_type: str | None = None,
    model_variants: dict | None = None,
    class_filter: list[int] | None = None,
    pipeline_stages: list[dict] | None = None,
    execution_unit: str | None = None,
    execution_scope: str = "bulk",
    async_job_id: str | None = None,
    frozen_model_ref: dict | None = None,
    task: str | None = None,
    prompt_type: str | None = None,
    accepted_inputs_protocol: str | None = None,
    accepted_input_ids: dict[str, str] | None = None,
    accepted_input_manifest_protocol: str | None = None,
    accepted_input_manifest_id: str | None = None,
    accepted_input_manifest_digest: str | None = None,
    accepted_video_inputs_protocol: str | None = None,
    accepted_video_manifest_id: str | None = None,
    accepted_video_selection_digest: str | None = None,
    accepted_video_selection: dict | None = None,
):
    frame_dispatcher = None
    if execution_unit == "frame":
        from app.workers.frame_preannotate import dispatch_frame_preannotate

        frame_dispatcher = dispatch_frame_preannotate

    request = BatchPredictionRequest(
        project_id=project_id,
        ml_backend_id=ml_backend_id,
        task_ids=task_ids,
        prompt=prompt,
        output_mode=output_mode,
        batch_id=batch_id,
        user_id=user_id,
        params=params,
        predict_mode=predict_mode,
        model_id=model_id,
        task_type=task_type,
        model_variants=model_variants,
        class_filter=class_filter,
        pipeline_stages=pipeline_stages,
        execution_unit=execution_unit,
        execution_scope=execution_scope,
        async_job_id=async_job_id,
        frozen_model_ref=frozen_model_ref,
        accepted_inputs_protocol=accepted_inputs_protocol,
        accepted_input_ids=accepted_input_ids,
        accepted_input_manifest_protocol=accepted_input_manifest_protocol,
        accepted_input_manifest_id=accepted_input_manifest_id,
        accepted_input_manifest_digest=accepted_input_manifest_digest,
        accepted_video_inputs_protocol=accepted_video_inputs_protocol,
        accepted_video_manifest_id=accepted_video_manifest_id,
        accepted_video_selection_digest=accepted_video_selection_digest,
        accepted_video_selection=accepted_video_selection,
        task=task,
        prompt_type=prompt_type,
    )
    try:
        asyncio.run(
            run_preannotation(
                request,
                execution_id=self.request.id,
                frame_dispatcher=frame_dispatcher,
            )
        )
    except (PreannotationInputDeferred, PreannotationResultDeferred) as exc:
        raise self.retry(exc=exc, countdown=30, max_retries=None) from exc
