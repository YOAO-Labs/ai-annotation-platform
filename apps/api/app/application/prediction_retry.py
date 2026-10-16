"""Failed-prediction retry admission, claim commits and dispatch compensation."""

import uuid

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.enums import PlatformRole
from app.db.models.async_job import AsyncJob
from app.db.models.ml_backend_registry import MLBackendRegistry as MLBackend
from app.db.models.prediction import FailedPrediction
from app.db.models.project import Project
from app.db.models.user import User
from app.schemas.async_job import AsyncJobRetryFailedResponse
from app.services import prediction as prediction_service
from app.services.pipeline_validation import resolve_preannotate_queue
from app.services.prediction_retry import MAX_RETRY_COUNT, PredictionRetryDispatcher

_RETRY_FAILED_KINDS = {"batch_predict"}


async def retry_prediction(
    db: AsyncSession,
    failed_id: uuid.UUID,
    user: User,
    *,
    dispatcher: PredictionRetryDispatcher,
) -> None:
    fp = await db.get(FailedPrediction, failed_id)
    if not fp:
        raise HTTPException(status_code=404, detail="Failed prediction not found")
    await prediction_service.assert_can_manage_failed_prediction(db, fp, user)
    if fp.dismissed_at is not None:
        raise HTTPException(
            status_code=409,
            detail="Failed prediction is dismissed; restore it before retry",
        )
    if (fp.retry_count or 0) >= MAX_RETRY_COUNT:
        raise HTTPException(
            status_code=409, detail=f"Max retries ({MAX_RETRY_COUNT}) exceeded"
        )
    fp = await prediction_service.claim_failed_prediction_retry(
        db, failed_id, MAX_RETRY_COUNT
    )
    if fp is None:
        raise HTTPException(status_code=409, detail="Retry already queued")

    device: str | None = None
    if fp.ml_backend_id is not None:
        backend = await db.get(MLBackend, fp.ml_backend_id)
        if backend and isinstance(backend.health_meta, dict):
            rp = (backend.health_meta.get("capabilities") or {}).get(
                "resource_profile"
            ) or {}
            device = rp.get("device")
            context = (fp.extra or {}).get("request_context") or {}
            from app.services.trained_models import controlled_device, is_controlled

            if is_controlled(context.get("model_id")) or context.get(
                "frozen_model_ref"
            ):
                device = controlled_device(backend)
    queue = resolve_preannotate_queue(
        [device],
        gpu_queue=settings.preannotate_gpu_queue,
        cpu_queue=settings.preannotate_cpu_queue,
    )
    await db.commit()
    try:
        dispatcher.dispatch_prediction_retry(failed_id, user.id, execution_queue=queue)
    except Exception:
        await prediction_service.release_failed_prediction_retry_claim(db, failed_id)
        await db.commit()
        raise


async def retry_job_items(
    db: AsyncSession,
    job_id: uuid.UUID,
    user: User,
    *,
    dispatcher: PredictionRetryDispatcher,
) -> AsyncJobRetryFailedResponse:
    job = await db.get(AsyncJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="async_job not found")
    if job.project_id is not None:
        project = await db.get(Project, job.project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="async_job not found")
        from app.services.project_access import resolve_project_access

        try:
            access = await resolve_project_access(db, user=user, project=project)
        except HTTPException:
            raise HTTPException(status_code=403, detail="not your job")
        if not access.is_manager:
            raise HTTPException(status_code=403, detail="requires project admin")
    elif user.role != PlatformRole.SUPER_ADMIN.value and job.user_id != user.id:
        raise HTTPException(status_code=403, detail="not your job")
    if job.kind not in _RETRY_FAILED_KINDS:
        raise HTTPException(
            status_code=400,
            detail=f"kind={job.kind} does not support failed item retry",
        )

    failed_ids = _payload_uuid_list(job.result or {}, "failed_prediction_ids")
    if not failed_ids:
        raise HTTPException(
            status_code=409,
            detail="no retryable failed prediction ids recorded for this job",
        )

    claimed_ids: list[uuid.UUID] = []
    for failed_id in failed_ids:
        if await prediction_service.claim_failed_prediction_retry(
            db, failed_id, MAX_RETRY_COUNT
        ):
            claimed_ids.append(failed_id)

    if not claimed_ids:
        raise HTTPException(
            status_code=409,
            detail="no retryable failed predictions remain for this job",
        )
    await db.commit()

    queued = 0
    try:
        for failed_id in claimed_ids:
            dispatcher.dispatch_job_item_retry(failed_id, user.id)
            queued += 1
    except Exception:
        for failed_id in claimed_ids[queued:]:
            await prediction_service.release_failed_prediction_retry_claim(
                db, failed_id
            )
        await db.commit()
        raise

    return AsyncJobRetryFailedResponse(
        status="queued", job_id=job_id, queued=queued, skipped=len(failed_ids) - queued
    )


def _payload_uuid_list(payload: dict, key: str) -> list[uuid.UUID]:
    value = payload.get(key)
    if not isinstance(value, list):
        return []
    out: list[uuid.UUID] = []
    for item in value:
        if not isinstance(item, str):
            continue
        try:
            out.append(uuid.UUID(item))
        except ValueError:
            continue
    return out
