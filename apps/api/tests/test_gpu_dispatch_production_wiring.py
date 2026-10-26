from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import redis
from aap_inference.gpu.contracts import GPUArbiterErrorCode
from aap_inference.tracker import TrackerSessionState
from aap_inference_contract.execution import (
    ExecutionFailure,
    ExecutionProvenance,
    ModelExecutionError,
    ModelExecutionObservation,
    ModelOperationResult,
    ModelTaskResult,
)
from app.services.video_tracking import adapters as tracker_impl_module
from app.services.video_tracking.adapters import (
    MLBackendVideoTrackerAdapter,
    TrackerContext,
)
from app.workers import frame_preannotate

from tests.test_frame_preannotate_persistence import (
    _install_video_gateway,
    _run_video_segment,
)
from tests.test_frame_preannotate_persistence import (
    frame_world as frame_world,
)


def _execution_result(
    operation,
    task_id: str,
    instance_id: uuid.UUID,
    *,
    scope_id: uuid.UUID | None = None,
    result: tuple = (),
    session_token: str | None = None,
):
    selected_pool_id = operation.selected_pool_id or operation.requested_pool_id
    return ModelOperationResult(
        request_id=operation.request_id,
        operation=operation.operation,
        results=(
            ModelTaskResult(
                task_id=task_id,
                result=result,
                session_token=session_token,
            ),
        ),
        provenance=ExecutionProvenance(
            requested_registry_id=operation.requested_registry_id,
            requested_pool_id=operation.requested_pool_id,
            registry_id=instance_id,
            pool_id=selected_pool_id,
            instance_id=instance_id,
            route_scope_id=scope_id or uuid.uuid4(),
            attempt_id=operation.request_id,
        ),
        observation=ModelExecutionObservation(
            backend_id=str(instance_id),
            operation=(
                "predict"
                if operation.operation == "batch_predict"
                else "predict_interactive"
            ),
            outcome="success",
            elapsed_seconds=0.0,
        ),
        wall_time_ms=0,
        response_digest="d" * 64 if operation.operation == "pinned_tracker" else None,
    )


@pytest.mark.asyncio
async def test_video_tracker_adapter_returns_immutable_session_and_provenance(
    monkeypatch,
):
    contexts: list[dict] = []
    port_arguments: list[dict] = []
    instance_id, scope_id = uuid.uuid4(), uuid.uuid4()
    scope = SimpleNamespace(id=scope_id, registry_id=instance_id, pool_id=None)

    class FakePort:
        async def execute(self, operation):
            contexts.append(operation.context)
            return _execution_result(
                operation,
                operation.task_inputs[0]["id"],
                instance_id,
                scope_id=scope_id,
                session_token="secret",
            )

    def port_factory(db, *, bound_route=None):
        port_arguments.append({"db": db, "bound_route": bound_route})
        return FakePort()

    monkeypatch.setattr(tracker_impl_module, "model_execution_port", port_factory)
    session = TrackerSessionState(span_start_frame=0, span_end_frame=9)
    db = object()
    context = TrackerContext(
        job_id=uuid.uuid4(),
        task_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        dataset_item_id=uuid.uuid4(),
        annotation_id=uuid.uuid4(),
        from_frame=0,
        to_frame=4,
        direction="forward",
        prompt={},
        source_geometry={},
        task_data={"id": "task"},
        db=db,
        bound_route=scope,
        requested_registry_id=instance_id,
        required_instance_id=instance_id,
        session=session,
    )
    propagation = await MLBackendVideoTrackerAdapter("sam2_video").propagate(context)
    assert contexts[0]["tracker_context"]["action"] == "start"
    assert session.action == "start" and session.token is None
    assert propagation.session.action == "continue"
    assert propagation.session.token == "secret"
    assert propagation.provenance.instance_id == instance_id
    assert propagation.response_digest == "d" * 64
    assert port_arguments == [{"db": db, "bound_route": scope}]


@pytest.mark.asyncio
async def test_frame_segment_does_not_pass_worker_session_factory_to_gateway(
    frame_world,
    monkeypatch,
) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    session_factories: list[object] = []
    port_arguments: list[dict] = []

    def tracked_session_factory(*args, **kwargs):
        factory = async_sessionmaker(*args, **kwargs)
        session_factories.append(factory)
        return factory

    class FakePort:
        async def execute(self, operation):
            return _execution_result(
                operation,
                operation.task_inputs[0]["id"],
                frame_world.backend_id,
            )

    def port_factory(db, *, bound_route=None):
        port_arguments.append({"db": db, "bound_route": bound_route})
        return FakePort()

    _install_video_gateway(monkeypatch, frame_world)
    monkeypatch.setattr(
        "sqlalchemy.ext.asyncio.async_sessionmaker", tracked_session_factory
    )
    monkeypatch.setattr(
        "app.services.inference_model_execution.model_execution_port", port_factory
    )
    monkeypatch.setattr(frame_preannotate, "_bump_progress", lambda *_args: (1, 1))
    monkeypatch.setattr(
        frame_preannotate, "_publish_frame_progress", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(frame_preannotate, "_write_job_progress", AsyncMock())

    stats = await _run_video_segment(frame_world, [1])

    assert stats["frames_done"] == 1
    assert stats["failed"] == 0
    assert len(session_factories) == 1
    assert len(port_arguments) == 1
    assert port_arguments[0]["bound_route"] is None


@pytest.mark.asyncio
async def test_frame_segment_aggregates_gpu_arbiter_failures(
    frame_world, monkeypatch
) -> None:
    class RejectedPort:
        async def execute(self, operation):
            raise ModelExecutionError(
                ExecutionFailure(
                    request_id=operation.request_id,
                    kind="admission",
                    code=GPUArbiterErrorCode.BACKEND_CONCURRENCY_SATURATED.value,
                    status_code=503,
                    detail={"message": "busy"},
                    retry_after="2",
                )
            )

    _install_video_gateway(monkeypatch, frame_world)
    monkeypatch.setattr(
        "app.services.inference_model_execution.model_execution_port",
        lambda *_args, **_kwargs: RejectedPort(),
    )
    monkeypatch.setattr(frame_preannotate, "_bump_progress", lambda *_args: (2, 2))
    monkeypatch.setattr(
        frame_preannotate, "_publish_frame_progress", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(frame_preannotate, "_write_job_progress", AsyncMock())

    stats = await _run_video_segment(frame_world, [1, 2])

    assert stats["failed"] == 2
    assert stats["gpu_arbiter_failures"] == [
        {
            "error_code": "gpu_backend_concurrency_saturated",
            "status_code": 503,
            "retry_after_s": 2,
            "count": 2,
        }
    ]


@pytest.mark.asyncio
async def test_frame_finalize_merges_gpu_arbiter_failures_into_job_result(
    db_session, monkeypatch
) -> None:
    from app.db.models.async_job import AsyncJob, AsyncJobStatus

    job = AsyncJob(
        kind="batch_predict",
        status=AsyncJobStatus.RUNNING.value,
        result={
            "existing": True,
            "frame_total": 5,
            "frame_dispatch": {
                "segment_ids": ["first", "second"],
                "total_frames": 5,
            },
        },
    )
    db_session.add(job)
    await db_session.commit()

    class FakeEngine:
        async def dispose(self) -> None:
            pass

    class FakeSessionFactory:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def __call__(self):
            class SessionContext:
                async def __aenter__(self):
                    return db_session

                async def __aexit__(self, *args):
                    return False

            return SessionContext()

    monkeypatch.setattr(
        "sqlalchemy.ext.asyncio.create_async_engine",
        lambda *args, **kwargs: FakeEngine(),
    )
    monkeypatch.setattr(
        "sqlalchemy.ext.asyncio.async_sessionmaker",
        FakeSessionFactory,
    )
    monkeypatch.setattr(frame_preannotate, "_bump_progress", lambda *_args: (4, 4))
    monkeypatch.setattr(
        frame_preannotate, "_publish_frame_progress", lambda *_args, **_kwargs: None
    )

    receipts = [
        {
            "frames_done": 1,
            "boxes": 2,
            "failed": 2,
            "skipped": 0,
            "gpu_arbiter_failures": [
                {
                    "error_code": "gpu_capacity_unavailable",
                    "status_code": 503,
                    "retry_after_s": 2,
                    "count": 2,
                }
            ],
        },
        {
            "frames_done": 0,
            "boxes": 0,
            "failed": 2,
            "skipped": 0,
            "gpu_arbiter_failures": [
                {
                    "error_code": "gpu_arbiter_not_ready",
                    "status_code": 503,
                    "retry_after_s": None,
                    "count": 1,
                },
                {
                    "error_code": "gpu_capacity_unavailable",
                    "status_code": 503,
                    "retry_after_s": 5,
                    "count": 1,
                },
            ],
        },
    ]
    with redis.from_url(frame_preannotate.settings.redis_url) as progress:
        key = frame_preannotate._segment_results_key(str(job.id))
        progress.hset(
            key,
            mapping={
                name: frame_preannotate._serialize_segment_stats(stats)
                for name, stats in zip(("first", "second"), receipts, strict=True)
            },
        )
    try:
        await frame_preannotate._finalize(
            [{"frames_done": 999, "boxes": 999}],
            project_id=str(uuid.uuid4()),
            job_id=str(job.id),
        )
    finally:
        with redis.from_url(frame_preannotate.settings.redis_url) as progress:
            progress.delete(key)

    await db_session.refresh(job)
    assert job.status == AsyncJobStatus.COMPLETED.value
    assert job.result["existing"] is True
    assert job.result["gpu_arbiter_failures"] == [
        {
            "error_code": "gpu_arbiter_not_ready",
            "status_code": 503,
            "retry_after_s": None,
            "count": 1,
        },
        {
            "error_code": "gpu_capacity_unavailable",
            "status_code": 503,
            "retry_after_s": 5,
            "count": 3,
        },
    ]
