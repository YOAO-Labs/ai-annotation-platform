"""Actual dispatch callbacks and result validation on frozen video stages."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace
import uuid

import pytest

from aap_inference.execution_pipeline import execute_task_pipeline
from aap_inference_contract.execution import (
    ExecutionProvenance,
    ModelExecutionDispatch,
    ModelExecutionObservation,
    ModelOperationResult,
    ModelTaskResult,
)
from app.services.content_snapshot import ContentError


def stage(number=7):
    return {
        "stage": number,
        "parent_stage": None,
        "ml_backend_id": str(uuid.uuid4()),
    }


def result(task_id):
    return SimpleNamespace(task_id=str(task_id), result=[], meta=None)


def _dispatch(operation, stage_number):
    request_id = operation.request_id
    return ModelExecutionDispatch(
        request_id=request_id,
        operation=operation.operation,
        route_scope_id=uuid.uuid4(),
        attempt_id=request_id,
        requested_registry_id=operation.requested_registry_id,
        requested_pool_id=operation.requested_pool_id,
        registry_id=operation.requested_registry_id,
        instance_id=operation.requested_registry_id,
        pool_id=None,
        member_id=None,
        topology_generation=1,
        endpoint_digest="0" * 64,
        capabilities={"models": [{"id": "track", "task": "tracker"}]},
    )


def _operation(operation, results, dispatch):
    registry_id = operation.requested_registry_id or uuid.uuid4()
    return ModelOperationResult(
        request_id=operation.request_id,
        operation=operation.operation,
        results=tuple(results),
        provenance=ExecutionProvenance(
            requested_registry_id=operation.requested_registry_id,
            requested_pool_id=operation.requested_pool_id,
            registry_id=registry_id,
            pool_id=None,
            instance_id=registry_id,
            route_scope_id=dispatch.route_scope_id,
            attempt_id=operation.request_id,
            topology_generation=dispatch.topology_generation,
            member_id=None,
            endpoint_sha256=dispatch.endpoint_digest,
        ),
        observation=ModelExecutionObservation(
            backend_id=str(registry_id),
            operation="predict",
            outcome="success",
            elapsed_seconds=0.0,
        ),
        wall_time_ms=0,
    )


class Port:
    def __init__(self, task_id, events, body):
        self.task_id = task_id
        self.events = events
        self.body = body

    async def execute(self, operation, *, before_dispatch=None):
        self.events.append("selected")
        dispatch = _dispatch(operation, 7)
        if before_dispatch is not None:
            await before_dispatch(dispatch)
        self.events.append("http")
        return await self.body(operation, dispatch)


@pytest.mark.asyncio
async def test_real_dispatch_callbacks_and_nonzero_root_validation_order():
    task = SimpleNamespace(id=uuid.uuid4())
    events = []
    original = ModelTaskResult(task_id=str(task.id), result=())
    converted = ModelTaskResult(task_id=str(task.id), result=())
    context = {"from_frame": 2, "to_frame": 5, "direction": "backward"}
    seen = {}

    async def body(operation, dispatch):
        assert operation.max_response_bytes == 64 * 1024**2
        assert operation.task_inputs == (
            {"id": str(task.id), "file_path": "fixed-version"},
        )
        assert operation.context["from_frame"] == 2
        seen["dispatch"] = dispatch
        return _operation(operation, (original,), dispatch)

    async def before(number, actual):
        assert number == 7 and actual is seen["dispatch"]
        assert actual.endpoint_digest == "0" * 64
        events.append("guard")

    async def validate(number, operation, wire):
        assert number == 7 and wire == context
        assert operation.results == (original,)
        events.append("validate")
        return (converted,)

    output = await execute_task_pipeline(
        str(task.id),
        [stage()],
        [Port(task.id, events, body)],
        [context],
        source_url="fixed-version",
        project_id=None,
        owner_prefix="test",
        route_operation="batch_predict",
        max_response_bytes=64 * 1024**2,
        validate_results=validate,
    )
    assert output.results == (converted,)
    assert events == ["selected", "guard", "http", "validate"]
    assert output.extra is None


@pytest.mark.asyncio
async def test_invalid_video_envelope_never_gets_child_dispatch():
    task = SimpleNamespace(id=uuid.uuid4())
    returned = result(task.id)
    returned.result = [{"type": "video_track_bbox", "marker": "untrusted"}]
    untouched = deepcopy(returned.result)
    events = []

    async def body(operation, dispatch):
        return _operation(
            operation,
            (ModelTaskResult(task_id=str(task.id), result=returned.result),),
            dispatch,
        )

    def validate(*_):
        raise ContentError("video_prediction_result_frame_invalid", 502)

    with pytest.raises(ContentError, match="frame_invalid"):
        await execute_task_pipeline(
            str(task.id),
            [stage()],
            [Port(task.id, events, body)],
            [None],
            source_url="fixed-version",
            project_id=None,
            owner_prefix="test",
            route_operation="batch_predict",
            validate_results=validate,
        )
    assert returned.result == untouched
    assert events == ["selected", "http"]


@pytest.mark.asyncio
async def test_repeated_cancellation_drains_actual_request_before_validation():
    task = SimpleNamespace(id=uuid.uuid4())
    started, release = asyncio.Event(), asyncio.Event()
    physical_done = False
    validated = False
    events = []

    async def body(operation, dispatch):
        nonlocal physical_done
        started.set()
        await release.wait()
        physical_done = True
        return _operation(
            operation,
            (ModelTaskResult(task_id=str(task.id), result=()),),
            dispatch,
        )

    def validate(*_):
        nonlocal validated
        validated = True

    pending = asyncio.create_task(
        execute_task_pipeline(
            str(task.id),
            [stage()],
            [Port(task.id, events, body)],
            [None],
            source_url="fixed-version",
            project_id=None,
            owner_prefix="test",
            route_operation="batch_predict",
            validate_results=validate,
        )
    )
    await asyncio.wait_for(started.wait(), 1)
    pending.cancel()
    await asyncio.sleep(0)
    pending.cancel()
    await asyncio.sleep(0)
    assert not pending.done() and not physical_done
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert physical_done and not validated
