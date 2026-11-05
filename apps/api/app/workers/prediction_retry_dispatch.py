"""Celery transport preserving single and recorded-batch retry placement."""

import uuid

from app.workers.predictions_retry import retry_failed_prediction


class CeleryPredictionRetryDispatcher:
    def dispatch_prediction_retry(
        self,
        failed_id: uuid.UUID,
        actor_id: uuid.UUID,
        *,
        execution_queue: str,
        job_id: uuid.UUID,
    ) -> None:
        options = {
            "queue": execution_queue,
            "task_id": str(job_id),
            "kwargs": {"job_id": str(job_id)},
        }
        retry_failed_prediction.apply_async(
            args=[str(failed_id), str(actor_id)], **options
        )

    def dispatch_job_item_retry(
        self,
        failed_id: uuid.UUID,
        actor_id: uuid.UUID,
        *,
        job_id: uuid.UUID,
    ) -> None:
        retry_failed_prediction.apply_async(
            args=[str(failed_id), str(actor_id)],
            kwargs={"job_id": str(job_id)},
            task_id=str(job_id),
        )
