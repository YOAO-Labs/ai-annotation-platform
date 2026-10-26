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

    def __init__(self, respond, *, instance_id=None, pool_id=None):
        self.respond = respond
        self.instance_id = instance_id
        self.pool_id = pool_id

    async def execute(self, operation: FrozenModelOperation) -> ModelOperationResult:
        actual_registry_id = self.instance_id or operation.requested_registry_id
        actual_pool_id = self.pool_id or operation.requested_pool_id
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
                route_scope_id=uuid.uuid4(),
                attempt_id=operation.request_id,
                topology_generation=operation.topology_generation,
                member_id=operation.selected_member_id,
            ),
            observation=ModelExecutionObservation(
                # The public observation carries the selected registry identity as
                # text; provenance keeps the UUID form.
                backend_id=str(actual_registry_id),
                operation="predict",
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
