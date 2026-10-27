from __future__ import annotations

import inspect
import uuid
from types import SimpleNamespace

from aap_inference_contract.execution import (
    ExecutionProvenance,
    FrozenModelOperation,
    ModelExecutionObservation,
    ModelOperationResult,
    ModelTaskResult,
)


def pipeline_output(
    results,
    *,
    instance_id=None,
    pool_id=None,
    requested_pool_id=None,
    extra=None,
    stage_stats=None,
):
    return SimpleNamespace(
        results=tuple(results),
        extra=extra,
        stage_stats=stage_stats,
        provenance=SimpleNamespace(
            instance_id=instance_id,
            registry_id=instance_id,
            pool_id=pool_id,
            requested_pool_id=requested_pool_id,
        ),
    )


class FakeModelExecutionPort:
    """Small immutable-result fake for consumer tests at the gateway boundary."""

    def __init__(self, respond, *, instance_id=None, pool_id=None, capabilities=None):
        self.respond = respond
        self.instance_id = instance_id
        self.pool_id = pool_id
        self.capabilities = capabilities

    async def execute(
        self, operation: FrozenModelOperation, *, before_dispatch=None
    ) -> ModelOperationResult:
        actual_registry_id = self.instance_id or operation.requested_registry_id
        actual_pool_id = self.pool_id or operation.requested_pool_id
        route_scope_id = uuid.uuid4()
        if before_dispatch is not None:
            from aap_inference_contract.execution import ModelExecutionDispatch

            health = operation.health_meta or {}
            await before_dispatch(
                ModelExecutionDispatch(
                    request_id=operation.request_id,
                    operation=operation.operation,
                    route_scope_id=route_scope_id,
                    attempt_id=operation.request_id,
                    requested_registry_id=operation.requested_registry_id,
                    requested_pool_id=operation.requested_pool_id,
                    registry_id=actual_registry_id,
                    instance_id=actual_registry_id,
                    pool_id=actual_pool_id,
                    member_id=operation.selected_member_id,
                    topology_generation=operation.topology_generation,
                    endpoint_digest="0" * 64,
                    capabilities=self.capabilities
                    if self.capabilities is not None
                    else health.get("capabilities"),
                )
            )
        results = []
        for task_input in operation.task_inputs:
            value = self.respond(operation, task_input)
            if inspect.isawaitable(value):
                value = await value
            if isinstance(value, ModelTaskResult):
                result = value
            else:
                result = ModelTaskResult(
                    task_id=str(task_input["id"]),
                    result=tuple(value),
                )
            results.append(result)
        return ModelOperationResult(
            request_id=operation.request_id,
            operation=operation.operation,
            results=tuple(results),
            provenance=ExecutionProvenance(
                requested_registry_id=operation.requested_registry_id,
                requested_pool_id=operation.requested_pool_id,
                registry_id=actual_registry_id,
                pool_id=actual_pool_id,
                instance_id=actual_registry_id,
                route_scope_id=route_scope_id,
                attempt_id=operation.request_id,
                topology_generation=operation.topology_generation,
                member_id=operation.selected_member_id,
                endpoint_sha256="0" * 64,
            ),
            observation=ModelExecutionObservation(
                # The public observation carries the selected registry identity as
                # text; provenance keeps the UUID form.
                backend_id=str(actual_registry_id),
                operation="predict"
                if operation.operation == "batch_predict"
                else "predict_interactive",
                outcome="success",
                elapsed_seconds=0.001,
            ),
            wall_time_ms=1,
        )


def patch_retry_execution(monkeypatch, execute):
    from app.application import durable_prediction_retry

    monkeypatch.setattr(
        durable_prediction_retry,
        "_model_execution_port",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr(durable_prediction_retry, "execute_task_pipeline", execute)


def patch_batch_execution(monkeypatch, respond, *, instance_id=None, pool_id=None):
    from app.application import preannotation_execution

    monkeypatch.setattr(
        preannotation_execution,
        "_model_execution_port",
        lambda *_args, **_kwargs: FakeModelExecutionPort(
            respond, instance_id=instance_id, pool_id=pool_id
        ),
    )


def _dispatch(operation, *, route_scope_id, instance_id, pool_id):
    from aap_inference_contract.execution import ModelExecutionDispatch

    health = operation.health_meta or {}
    return ModelExecutionDispatch(
        request_id=operation.request_id,
        operation=operation.operation,
        route_scope_id=route_scope_id,
        attempt_id=operation.request_id,
        requested_registry_id=operation.requested_registry_id,
        requested_pool_id=operation.requested_pool_id,
        registry_id=instance_id,
        instance_id=instance_id,
        pool_id=pool_id,
        member_id=operation.selected_member_id,
        topology_generation=operation.topology_generation,
        endpoint_digest="0" * 64,
        capabilities=health.get("capabilities"),
    )


def _task_result(value):
    if isinstance(value, ModelTaskResult):
        return value
    raw_result = getattr(value, "result")
    return ModelTaskResult(
        task_id=str(getattr(value, "task_id")),
        result=tuple(raw_result),
        score=getattr(value, "score", None),
        model_version=getattr(value, "model_version", None),
        inference_time_ms=getattr(value, "inference_time_ms", None),
        cache_hit=getattr(value, "cache_hit", None),
        model_load_ms=getattr(value, "model_load_ms", None),
        meta=getattr(value, "meta", None),
        mask_input_next=getattr(value, "mask_input_next", None),
        diagnostic=getattr(value, "diagnostic", None),
    )


class PredictionClientExecutionPort:
    """Adapt a batch-shaped fake client to the frozen model execution port.

    Several consumer regressions stub ``predict(inputs, context)`` because that
    was the old transport boundary. The pipeline now owns that boundary; this
    adapter keeps the fake focused on model responses while still invoking the
    real dispatch callback at the owner-defined boundary.
    """

    def __init__(self, client, *, instance_id=None, pool_id=None):
        self.client = client
        self.instance_id = instance_id
        self.pool_id = pool_id

    async def execute(
        self, operation: FrozenModelOperation, *, before_dispatch=None
    ) -> ModelOperationResult:
        from aap_inference_contract.execution import thaw_json

        actual_registry_id = self.instance_id or operation.requested_registry_id
        actual_pool_id = self.pool_id or operation.requested_pool_id
        route_scope_id = uuid.uuid4()
        if before_dispatch is not None:
            await before_dispatch(
                _dispatch(
                    operation,
                    route_scope_id=route_scope_id,
                    instance_id=actual_registry_id,
                    pool_id=actual_pool_id,
                )
            )
        inputs = [thaw_json(item) for item in operation.task_inputs]
        context = None if operation.context is None else thaw_json(operation.context)
        raw_results = await self.client.predict(inputs, context=context)
        return ModelOperationResult(
            request_id=operation.request_id,
            operation=operation.operation,
            results=tuple(_task_result(value) for value in raw_results),
            provenance=ExecutionProvenance(
                requested_registry_id=operation.requested_registry_id,
                requested_pool_id=operation.requested_pool_id,
                registry_id=actual_registry_id,
                pool_id=actual_pool_id,
                instance_id=actual_registry_id,
                route_scope_id=route_scope_id,
                attempt_id=operation.request_id,
                topology_generation=operation.topology_generation,
                member_id=operation.selected_member_id,
                endpoint_sha256="0" * 64,
            ),
            observation=ModelExecutionObservation(
                backend_id=str(actual_registry_id),
                operation="predict",
                outcome="success",
                elapsed_seconds=0.001,
            ),
            wall_time_ms=1,
        )


async def run_client_task_pipeline(
    task,
    stages,
    clients,
    stage_contexts,
    *,
    resolve_url,
    stage_modes=None,
    load_image=None,
    input_image_path=None,
    project_id=None,
    owner_prefix="test",
    route_operation="test",
    before_dispatch=None,
    upload_crop=None,
    execution_snapshot_id=None,
    instance_id=None,
    pool_id=None,
):
    """Run the current portable pipeline with batch-shaped fake clients."""

    from PIL import Image

    from aap_inference.execution_pipeline import (
        execute_task_pipeline,
        resolve_input_mode,
    )
    from aap_inference_contract.execution import thaw_json

    modes = tuple(stage_modes or (resolve_input_mode(stage) for stage in stages))
    source_image = None
    if len(stages) > 1 and (
        any(mode == "crop" for mode in modes[1:]) or load_image is not None
    ):
        if input_image_path is not None:
            with Image.open(input_image_path) as opened:
                source_image = opened.convert("RGB")
        elif load_image is not None:
            source_image = load_image(task)
        else:
            from app.services.prediction_context import load_task_image

            source_image = load_task_image(task)

    crop_publisher = None
    if upload_crop is not None:
        from app.application.preannotation_execution import _CropPublishPort

        crop_publisher = _CropPublishPort(upload_crop)

    result = await execute_task_pipeline(
        str(task.id),
        stages,
        [
            PredictionClientExecutionPort(
                client, instance_id=instance_id, pool_id=pool_id
            )
            for client in clients
        ],
        stage_contexts,
        source_url=resolve_url(task),
        project_id=project_id,
        owner_prefix=owner_prefix,
        route_operation=route_operation,
        stage_modes=modes,
        source_image=source_image,
        crop_publisher=crop_publisher,
        before_dispatch=before_dispatch,
        execution_snapshot_id=execution_snapshot_id,
    )
    stats = (
        None
        if result.stage_stats is None
        else {stage: dict(values) for stage, values in result.stage_stats.items()}
    )
    return result.results, thaw_json(result.extra), stats


async def run_batch_request(**kwargs):
    """Execute a batch request through the application owner with real commits."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.application.preannotation_contract import BatchPredictionRequest
    from app.application.preannotation_execution import _execute_preannotation
    from app.config import settings

    execution_id = kwargs.pop("celery_task_id", None)
    frame_dispatcher = kwargs.pop("frame_dispatcher", None)
    kwargs.pop("worker_node_id", None)
    engine = create_async_engine(settings.database_url, echo=False)
    try:
        return await _execute_preannotation(
            BatchPredictionRequest(**kwargs),
            execution_id=execution_id,
            frame_dispatcher=frame_dispatcher,
            SessionLocal=async_sessionmaker(engine, expire_on_commit=False),
        )
    finally:
        await engine.dispose()
