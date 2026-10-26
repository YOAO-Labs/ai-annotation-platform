"""Celery adapter for the application batch-prediction dispatch port."""

from app.application.preannotation_contract import BatchPredictionRequest


class CeleryBatchPredictionDispatcher:
    def dispatch(
        self,
        request: BatchPredictionRequest,
        *,
        queue: str,
        soft_time_limit: int | None = None,
    ) -> str:
        from app.services.jobs_queue import _batch_prediction_task_payload
        from app.workers.tasks import batch_predict

        args, kwargs = _batch_prediction_task_payload(request)

        options: dict[str, str | int] = {"queue": queue}
        if request.async_job_id is not None:
            options["task_id"] = request.async_job_id
        if soft_time_limit is not None:
            options["soft_time_limit"] = soft_time_limit
        result = batch_predict.apply_async(
            args=args,
            kwargs=kwargs,
            **options,
        )
        return str(result.id)
