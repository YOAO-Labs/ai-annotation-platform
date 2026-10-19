"""Failed-prediction retry admission, claim commits and dispatch compensation."""

import json
import uuid

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.async_job_contracts import (
    AsyncJobOperationFailure,
    PredictionRetryPort,
)
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


class SqlPredictionRetry(PredictionRetryPort):
    """Bind failed-prediction retry workflow to its caller-owned SQL session."""

    def __init__(
        self, db: AsyncSession, *, dispatcher: PredictionRetryDispatcher
    ) -> None:
        self._db = db
        self._dispatcher = dispatcher

    async def retry_prediction(
        self, *, failed_id: uuid.UUID, actor_id: uuid.UUID
    ) -> None:
        try:
            db = self._db
            fp = await db.get(FailedPrediction, failed_id)
            if not fp:
                raise HTTPException(
                    status_code=404, detail="Failed prediction not found"
                )
            await prediction_service.assert_can_manage_failed_prediction(
                db, fp, actor_id
            )
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
                    from app.services.trained_models import (
                        controlled_device,
                        is_controlled,
                    )

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
                self._dispatcher.dispatch_prediction_retry(
                    failed_id, actor_id, execution_queue=queue
                )
            except Exception:
                await prediction_service.release_failed_prediction_retry_claim(
                    db, failed_id
                )
                await db.commit()
                raise
        except HTTPException as exc:
            raise AsyncJobOperationFailure(
                exc.status_code, exc.detail, headers=exc.headers
            ) from exc

    async def retry_job_items(
        self, *, job_id: uuid.UUID, actor_id: uuid.UUID
    ) -> AsyncJobRetryFailedResponse:
        try:
            db = self._db
            job = await db.get(AsyncJob, job_id)
            if job is None:
                raise HTTPException(status_code=404, detail="async_job not found")
            if job.project_id is not None:
                project = await db.get(Project, job.project_id)
                if project is None:
                    raise HTTPException(status_code=404, detail="async_job not found")
                user = await db.get(User, actor_id)
                if user is None:
                    raise HTTPException(status_code=403, detail="not your job")
                from app.services.project_access import resolve_project_access

                try:
                    access = await resolve_project_access(
                        db, actor_id=user.id, project=project
                    )
                except HTTPException:
                    raise HTTPException(status_code=403, detail="not your job")
                if not access.is_manager:
                    raise HTTPException(
                        status_code=403, detail="requires project admin"
                    )
            else:
                user = await db.get(User, actor_id, populate_existing=True)
                if (
                    user is None
                    or not user.is_active
                    or (
                        user.role != PlatformRole.SUPER_ADMIN.value
                        and job.user_id != actor_id
                    )
                ):
                    raise HTTPException(status_code=403, detail="not your job")
            if job.kind not in _RETRY_FAILED_KINDS:
                raise HTTPException(
                    status_code=400,
                    detail=f"kind={job.kind} does not support failed item retry",
                )

            failed_ids = await _recorded_failed_ids(db, job)
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
                    self._dispatcher.dispatch_job_item_retry(failed_id, actor_id)
                    queued += 1
            except Exception:
                for failed_id in claimed_ids[queued:]:
                    await prediction_service.release_failed_prediction_retry_claim(
                        db, failed_id
                    )
                await db.commit()
                raise

            return AsyncJobRetryFailedResponse(
                status="queued",
                job_id=job_id,
                queued=queued,
                skipped=len(failed_ids) - queued,
            )
        except HTTPException as exc:
            raise AsyncJobOperationFailure(
                exc.status_code, exc.detail, headers=exc.headers
            ) from exc


async def _recorded_failed_ids(db: AsyncSession, job: AsyncJob) -> list[uuid.UUID]:
    result = job.result or {}
    if "batch_result_scope" not in result:
        return _payload_uuid_list(result, "failed_prediction_ids")
    from aap_preannotation_results.contract import (
        BatchResultFailure,
        ReadBatchResultExecutionRequest,
        Scope,
    )
    from pydantic import ValidationError
    from app.services.batch_result_data import SqlAlchemyBatchResultData

    try:
        scope = Scope.model_validate_json(json.dumps(result["batch_result_scope"]))
    except (ValidationError, TypeError, ValueError):
        raise HTTPException(
            status_code=409, detail="batch result history is invalid"
        ) from None
    if scope.job_id != job.id or scope.project_id != (
        job.project_id or job.retained_project_id
    ):
        raise HTTPException(
            status_code=409, detail="batch result history scope changed"
        )
    ids = []
    cursor = 0
    try:
        while True:
            page = await SqlAlchemyBatchResultData(db).call(
                "read_batch_result_execution",
                ReadBatchResultExecutionRequest(
                    **scope.model_dump(),
                    after_ordinal=cursor,
                    limit=100,
                ),
            )
            ids.extend(
                member.failed_prediction_id
                for member in page.members
                if member.failed_prediction_id is not None
            )
            if page.next_ordinal is None:
                return ids
            cursor = page.next_ordinal
    except BatchResultFailure:
        raise HTTPException(
            status_code=503, detail="batch result history unavailable"
        ) from None


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
