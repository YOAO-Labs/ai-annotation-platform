"""Failed-prediction admission and durable publication of stable retry Jobs."""

import json
import uuid

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.async_job_contracts import (
    AsyncJobOperationFailure,
    PredictionRetryPort,
)
from app.services.project_access import get_workspace_project
from app.config import settings
from app.db.enums import PlatformRole
from app.db.models.async_job import AsyncJob
from app.services.ml_backend import MLBackendService
from app.schemas.async_job import AsyncJobRetryFailedResponse
from app.services import async_job as jobs, prediction as prediction_service
from app.services.identity_account_lock_data import IdentityAccountLockData
from app.services.pipeline_validation import resolve_preannotate_queue
from app.services.prediction_retry import MAX_RETRY_COUNT, PredictionRetryDispatcher
from app.services.labeling_core_read_relations import (
    failed_prediction_row,
    get_core_row,
    task_row,
)

_RETRY_FAILED_KINDS = {"batch_predict"}


class SqlPredictionRetry(PredictionRetryPort):
    """Bind failed-prediction retry workflow to its caller-owned SQL session."""

    def __init__(
        self, db: AsyncSession, *, dispatcher: PredictionRetryDispatcher
    ) -> None:
        self._db = db
        self._dispatcher = dispatcher

    async def _admit(self, failed_id, actor_id, execution_queue):
        from aap_preannotation_results.retry_contract import (
            AdmitPredictionRetryRequest,
            PredictionRetryFailure,
        )
        from app.services.prediction_retry_data import SqlAlchemyPredictionRetryData

        try:
            result = await SqlAlchemyPredictionRetryData(self._db, writer=True).call(
                "admit_prediction_retry",
                AdmitPredictionRetryRequest(
                    job_id=uuid.uuid4(),
                    failed_prediction_id=failed_id,
                    actor_id=actor_id,
                    execution_queue=execution_queue,
                ),
            )
        except PredictionRetryFailure as error:
            _raise_retry_failure(error.code)
        # Display metadata stays optional; authority and immutable parameters
        # were already accepted by the native owners in this same transaction.
        metadata = {}
        attempt = result.attempt
        task = (
            await get_core_row(self._db, task_row, attempt.task_id)
            if attempt.task_id
            else None
        )
        project = await get_workspace_project(self._db, project_id=attempt.project_id)
        registry = (
            await MLBackendService(self._db).get_registry(attempt.ml_backend_id)
            if attempt.ml_backend_id
            else None
        )
        if task is not None:
            metadata["task_display_id"] = task.display_id
        if project is not None:
            metadata.update(
                project_display_id=project.display_id, project_name=project.name
            )
        if registry is not None:
            metadata["ml_backend_name"] = registry.name
        if metadata:
            await jobs.update_payload(self._db, attempt.job_id, metadata)
        return attempt

    async def retry_prediction(
        self, *, failed_id: uuid.UUID, actor_id: uuid.UUID
    ) -> None:
        try:
            db = self._db
            fp = await get_core_row(db, failed_prediction_row, failed_id)
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
            device: str | None = None
            if fp.ml_backend_id is not None:
                registry = await MLBackendService(db).get_registry(fp.ml_backend_id)
                if registry is not None:
                    health_meta = registry.health_meta or {}
                    rp = (health_meta.get("capabilities") or {}).get(
                        "resource_profile"
                    ) or {}
                    device = rp.get("device")
                    extra = fp.extra if isinstance(fp.extra, dict) else {}
                    context = extra.get("request_context")
                    context = context if isinstance(context, dict) else {}
                    from app.services.trained_models import (
                        controlled_device,
                        is_controlled,
                    )

                    if is_controlled(context.get("model_id")) or context.get(
                        "frozen_model_ref"
                    ):
                        device = controlled_device(registry)
            queue = resolve_preannotate_queue(
                [device],
                gpu_queue=settings.preannotate_gpu_queue,
                cpu_queue=settings.preannotate_cpu_queue,
            )
            attempt = await self._admit(failed_id, actor_id, queue)
            await db.commit()
            from app.application.prediction_retry_delivery import publish_admitted_retry

            await publish_admitted_retry(
                async_sessionmaker(db.bind, expire_on_commit=False),
                self._dispatcher,
                attempt.job_id,
            )
        except HTTPException as exc:
            raise AsyncJobOperationFailure(
                exc.status_code, exc.detail, headers=exc.headers
            ) from exc

    async def retry_job_items(
        self, *, job_id: uuid.UUID, actor_id: uuid.UUID
    ) -> AsyncJobRetryFailedResponse:
        try:
            db = self._db
            job = await jobs.get_job(db, job_id)
            if job is None:
                raise HTTPException(status_code=404, detail="async_job not found")
            if job.project_id is not None:
                project = await get_workspace_project(db, project_id=job.project_id)
                if project is None:
                    raise HTTPException(status_code=404, detail="async_job not found")
                actor = await IdentityAccountLockData(db).read_actor_status(actor_id)
                if actor is None:
                    raise HTTPException(status_code=403, detail="not your job")
                from app.services.project_access import resolve_project_access

                try:
                    access = await resolve_project_access(
                        db, actor_id=actor.id, project=project
                    )
                except HTTPException:
                    raise HTTPException(status_code=403, detail="not your job")
                if not access.is_manager:
                    raise HTTPException(
                        status_code=403, detail="requires project admin"
                    )
            else:
                actor = await IdentityAccountLockData(db).read_actor_status(actor_id)
                if (
                    actor is None
                    or not actor.is_active
                    or (
                        actor.role != PlatformRole.SUPER_ADMIN.value
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

            attempts = []
            for failed_id in failed_ids:
                try:
                    async with db.begin_nested():
                        attempt = await self._admit(failed_id, actor_id, None)
                except HTTPException as error:
                    if error.status_code in {404, 409}:
                        continue
                    raise
                attempts.append(attempt)

            if not attempts:
                raise HTTPException(
                    status_code=409,
                    detail="no retryable failed predictions remain for this job",
                )
            await db.commit()

            from app.application.prediction_retry_delivery import publish_admitted_retry

            factory = async_sessionmaker(db.bind, expire_on_commit=False)
            for attempt in attempts:
                await publish_admitted_retry(factory, self._dispatcher, attempt.job_id)

            return AsyncJobRetryFailedResponse(
                status="queued",
                job_id=job_id,
                queued=len(attempts),
                skipped=len(failed_ids) - len(attempts),
            )
        except HTTPException as exc:
            raise AsyncJobOperationFailure(
                exc.status_code, exc.detail, headers=exc.headers
            ) from exc


def _raise_retry_failure(code):
    status, detail = {
        "not_found": (404, "Failed prediction not found"),
        "account_not_active": (401, "账号已停用或不存在"),
        "permission_denied": (403, "requires project admin"),
        "already_pending": (409, "Retry already queued"),
        "retry_limit": (409, f"Max retries ({MAX_RETRY_COUNT}) exceeded"),
        "dismissed": (409, "Failed prediction is dismissed; restore it before retry"),
        "scope_mismatch": (409, "Retry admission scope changed"),
    }.get(code, (503, "Retry admission is unavailable"))
    raise HTTPException(status_code=status, detail=detail)


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
