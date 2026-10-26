from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import redis
from aap_inference.backend_transport import PredictionResult
from app.services.gpu_arbitration.contracts import (
    GPUArbiterDispatchError,
    GPUArbiterErrorCode,
)
from app.services.video_tracking import adapters as tracker_impl_module
from app.services.video_tracking.adapters import (
    MLBackendVideoTrackerAdapter,
    TrackerContext,
    TrackerSession,
)
from app.workers import frame_preannotate

from tests.test_frame_preannotate_persistence import (
    _install_video_gateway,
    _run_video_segment,
)
from tests.test_frame_preannotate_persistence import (
    frame_world as frame_world,
)


@pytest.mark.asyncio
async def test_video_tracker_adapter_updates_opaque_session_token(monkeypatch) -> None:
    contexts: list[dict] = []

    class FakeClient:
        def __init__(self, _backend, **_kwargs) -> None:
            pass

        async def predict_interactive(self, task_data, context):
            contexts.append(context)
            return SimpleNamespace(
                result=[],
                meta={"tracker_context": {"mode": "session", "token": "secret"}},
            )

    monkeypatch.setattr(tracker_impl_module, "MLBackendClient", FakeClient)
    session = TrackerSession(span_start_frame=0, span_end_frame=9)
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
        ml_backend=SimpleNamespace(state="connected"),
        session=session,
    )

    assert [
        item
        async for item in MLBackendVideoTrackerAdapter("sam2_video").propagate(context)
    ] == []
    assert contexts[0]["tracker_context"]["action"] == "start"
    assert session.action == "continue"
    assert session.token == "secret"


@pytest.mark.asyncio
async def test_frame_segment_builds_authority_from_its_own_session_factory(
    frame_world,
    monkeypatch,
) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    authority_marker = object()
    client_kwargs: list[dict] = []
    built_from: list[object] = []
    session_factories: list[object] = []

    def tracked_session_factory(*args, **kwargs):
        factory = async_sessionmaker(*args, **kwargs)
        session_factories.append(factory)
        return factory

    def build_authority(received_factory):
        built_from.append(received_factory)
        return authority_marker

    class FakeClient:
        def __init__(self, _db, backend, **kwargs):
            assert backend.id == frame_world.backend_id
            client_kwargs.append(kwargs)
            self.last_instance_id = backend.id
            self.pool_id = None

        async def predict(self, *_args, **_kwargs):
            return [PredictionResult(task_id=str(frame_world.task_id), result=[])]

    _install_video_gateway(monkeypatch, frame_world)
    monkeypatch.setattr(
        "sqlalchemy.ext.asyncio.async_sessionmaker", tracked_session_factory
    )
    monkeypatch.setattr(
        "app.services.gpu_arbitration.dispatch.build_gpu_dispatch_context_factory",
        build_authority,
    )
    monkeypatch.setattr(
        "app.services.ml_routing.client.RoutedMLBackendClient", FakeClient
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
    assert built_from
    assert all(factory is session_factories[0] for factory in built_from)
    assert len(client_kwargs) == 1
    assert client_kwargs[0]["shadow_session_factory"] is session_factories[0]
    assert client_kwargs[0]["dispatch_context_factory"] is authority_marker
    assert client_kwargs[0]["operation"] == "frame_predict"


@pytest.mark.asyncio
async def test_frame_segment_aggregates_gpu_arbiter_failures(
    frame_world, monkeypatch
) -> None:
    class RejectedClient:
        def __init__(self, _db, backend, **_kwargs):
            self.last_instance_id = backend.id
            self.pool_id = None

        async def predict(self, inputs, context=None):
            raise GPUArbiterDispatchError(
                GPUArbiterErrorCode.BACKEND_CONCURRENCY_SATURATED,
                message="busy",
                retry_after_s=2,
            )

    _install_video_gateway(monkeypatch, frame_world)
    monkeypatch.setattr(
        "app.services.gpu_arbitration.dispatch.build_gpu_dispatch_context_factory",
        lambda factory: object(),
    )
    monkeypatch.setattr(
        "app.services.ml_routing.client.RoutedMLBackendClient", RejectedClient
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
