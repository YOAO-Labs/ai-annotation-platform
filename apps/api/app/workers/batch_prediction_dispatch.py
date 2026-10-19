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
        from app.workers.tasks import batch_predict

        kwargs = {
            "prompt": request.prompt,
            "output_mode": request.output_mode,
            "batch_id": request.batch_id,
            "user_id": request.user_id,
            "params": request.params,
            "predict_mode": request.predict_mode,
            "model_id": request.model_id,
            "task": request.task,
            "prompt_type": request.prompt_type,
            "model_variants": request.model_variants,
            "class_filter": request.class_filter,
            "pipeline_stages": request.pipeline_stages,
            "execution_unit": request.execution_unit,
            "execution_scope": request.execution_scope,
        }
        if request.task_type is not None:
            kwargs["task_type"] = request.task_type
        if request.async_job_id is not None:
            kwargs["async_job_id"] = request.async_job_id
        if request.frozen_model_ref is not None:
            kwargs["frozen_model_ref"] = request.frozen_model_ref
        if request.accepted_inputs_protocol is not None:
            kwargs["accepted_inputs_protocol"] = request.accepted_inputs_protocol
        if request.accepted_input_ids is not None:
            kwargs["accepted_input_ids"] = request.accepted_input_ids

        options: dict[str, str | int] = {"queue": queue}
        if request.async_job_id is not None:
            options["task_id"] = request.async_job_id
        if soft_time_limit is not None:
            options["soft_time_limit"] = soft_time_limit
        result = batch_predict.apply_async(
            args=[request.project_id, request.ml_backend_id, request.task_ids],
            kwargs=kwargs,
            **options,
        )
        return str(result.id)
